import os
import time
import random
import re
from typing import List, Dict

from django.core.management.base import BaseCommand
from django.db import transaction
from openai import OpenAI

from chatbot.models import SimulatedCustomer, SimulatedConversation, SimulatedMessage


WAITER_SYSTEM = (
    "You are a restaurant waiter. Follow this fixed protocol:\n"
    "1) Welcome the customer and ask if they had a good day.\n"
    "2) Ask what the customer's top 3 favourite foods are.\n"
    "3) Ask what dish(es) they want to order today.\n"
    "Be concise, polite, and ask exactly one question per turn."
)

CUSTOMER_SYSTEM = (
    "You are a restaurant customer. You respond naturally.\n"
    "When asked about your top 3 favourite foods, answer with exactly three different foods.\n"
    "Additionally, you have a fixed dietary label: omnivore, vegetarian, or vegan.\n"
    "If vegetarian: no meat/fish. If vegan: no animal products.\n"
    "When asked what you want to order, choose dish(es) consistent with your dietary label.\n"
    "Return answers in plain natural language."
)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


# -----------------------------------------------------------------------------
# OpenAI call helper (YOU WERE MISSING THIS)
# -----------------------------------------------------------------------------
def call_agent(client: OpenAI, system: str, transcript: List[Dict], turn_instruction: str = "") -> str:
    conv_text = ""
    for m in transcript:
        conv_text += f"{m['role'].upper()}: {m['content']}\n"

    prompt = (
        f"{system}\n"
        f"{turn_instruction}\n\n"
        f"Conversation so far:\n{conv_text}\n\n"
        "Your next reply:"
    )

    resp = client.responses.create(model=MODEL, input=prompt)
    return (resp.output_text or "").strip()


# -----------------------------------------------------------------------------
# Dietary label sampling
# -----------------------------------------------------------------------------
def sample_label(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.02:
        return "vegan"
    if r < 0.2:
        return "vegetarian"
    return "omnivore"


# -----------------------------------------------------------------------------
# Keep label fixed
# -----------------------------------------------------------------------------
def inject_customer_system_with_label(base_system: str, label: str) -> str:
    return (
        base_system
        + "\n\n"
        + "IMPORTANT:\n"
        + f"- Your dietary label is strictly '{label}' for the entire conversation.\n"
        + "- Only answer the question you were just asked.\n"
        + "- Do NOT mention your dietary label explicitly unless the waiter asks.\n"
    )


# -----------------------------------------------------------------------------
# Robust natural parser
# -----------------------------------------------------------------------------
def parse_three_foods_natural(text: str) -> List[str]:
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty foods answer")

    # normalize unicode punctuation (I’d -> I'd)
    s = (
        s.replace("\u2019", "'")
         .replace("\u2018", "'")
         .replace("\u201c", '"')
         .replace("\u201d", '"')
    )

    # remove common leading phrases
    s = re.sub(r"^(sure!?|yeah!?|yes!?|well,?|honestly,?)\s*", "", s, flags=re.I)
    s = re.sub(r"^i(?:'d| would)\s+say\s+", "", s, flags=re.I)
    s = re.sub(
        r"^(?:i(?:'d| would)\s+say\s+)?my\s+(?:top\s+)?(?:three\s+)?"
        r"(?:favorite|favourite)\s+foods\s+(?:are|would be)\s+",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"^(i (really )?(love|like|enjoy))\s+", "", s, flags=re.I)

    # remove "of course" globally
    s = re.sub(r"\bof course\b,?\s*", "", s, flags=re.I)

    # normalize separators
    s_norm = (
        s.replace(" and ", ", ")
         .replace(" & ", ", ")
         .replace(";", ", ")
         .replace("/", ", ")
         .replace("\n", ", ")
    )

    parts = [p.strip(" \t\r\n.,;:!?'\"") for p in s_norm.split(",")]
    parts = [p for p in parts if p]

    DISCOURSE = {
        "of course", "sure", "yeah", "yes", "certainly",
        "definitely", "absolutely", "for sure"
    }

    cleaned: List[str] = []
    seen = set()
    MAX_LEN = 60

    for p in parts:
        item = p.strip()

        # cut trailing sentence
        item = re.sub(r"[.!?].*$", "", item).strip()

        # strip leading "a good "
        item = re.sub(r"^a\s+good\s+", "", item, flags=re.I).strip()

        # handle "X like Y"
        low = item.lower()
        if " like " in low:
            item = item.split(" like ", 1)[1].strip()
        elif " such as " in low:
            item = item.split(" such as ", 1)[1].strip()

        # remove trailing clauses
        item = re.sub(r"\b(they|which|that)\b.*$", "", item, flags=re.I).strip()

        # remove filler verbs again
        item = re.sub(r"^(i (really )?(love|like|enjoy))\s+", "", item, flags=re.I).strip()

        if not item:
            continue

        if item.lower() in DISCOURSE:
            continue

        if len(item) > MAX_LEN:
            continue

        key = item.lower()
        if key in seen:
            continue

        seen.add(key)
        cleaned.append(item)

        if len(cleaned) == 3:
            break

    if len(cleaned) != 3:
        raise ValueError(f"Could not extract exactly 3 foods from answer: {text!r}")

    return cleaned


# -----------------------------------------------------------------------------
# Index helper
# -----------------------------------------------------------------------------
def _next_customer_index() -> int:
    codes = SimulatedCustomer.objects.values_list("customer_code", flat=True)
    max_idx = 0
    for code in codes:
        if not code:
            continue
        m = re.match(r"^CUST_(\d+)$", code)
        if m:
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except Exception:
                continue
    return max_idx + 1


# -----------------------------------------------------------------------------
# Main command
# -----------------------------------------------------------------------------
class Command(BaseCommand):
    help = "Simulate waiter-customer conversations and store them in the database."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=100)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--start", type=int, default=0)

    @transaction.atomic
    def handle(self, *args, **opts):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        client = OpenAI(api_key=api_key)

        n = int(opts["n"])
        pause = float(opts["sleep"])
        seed = int(opts["seed"])
        start_arg = int(opts["start"])

        start_idx = start_arg if start_arg > 0 else _next_customer_index()
        end_idx = start_idx + n - 1

        rng = random.Random(seed + start_idx)

        self.stdout.write(f"Simulating customers {start_idx}..{end_idx} (n={n}).")

        for i in range(start_idx, end_idx + 1):
            cust_code = f"CUST_{i:03d}"

            try:
                customer, _ = SimulatedCustomer.objects.get_or_create(customer_code=cust_code)

                target_label = sample_label(rng)
                customer_system = inject_customer_system_with_label(CUSTOMER_SYSTEM, target_label)

                conversation = SimulatedConversation.objects.create(customer=customer)

                transcript: List[Dict] = []
                turn = 1

                # Turn 1
                waiter_1 = call_agent(
                    client, WAITER_SYSTEM, transcript,
                    "This turn: step 1 only (welcome + ask about the day)."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter",
                    content=waiter_1, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_1})
                turn += 1

                # Turn 2
                customer_1 = call_agent(
                    client, customer_system, transcript,
                    "Answer naturally in 1-2 sentences about your day."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer",
                    content=customer_1, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_1})
                customer.day_summary = customer_1[:5000]
                turn += 1

                # Turn 3
                waiter_2 = call_agent(
                    client, WAITER_SYSTEM, transcript,
                    "This turn: step 2 only (ask top 3 favourite foods)."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter",
                    content=waiter_2, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_2})
                turn += 1

                # Turn 4
                customer_2 = call_agent(
                    client, customer_system, transcript,
                    "Answer with your top 3 favourite foods in a natural way."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer",
                    content=customer_2, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_2})
                turn += 1

                foods = parse_three_foods_natural(customer_2)

                customer.favorite_foods = foods
                customer.dietary_label = target_label
                customer.save(update_fields=["day_summary", "favorite_foods", "dietary_label"])

                # Turn 5
                waiter_3 = call_agent(
                    client, WAITER_SYSTEM, transcript,
                    "This turn: step 3 only (ask what they want to order)."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter",
                    content=waiter_3, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_3})
                turn += 1

                # Turn 6
                customer_3 = call_agent(
                    client, customer_system, transcript,
                    "Answer what you would order today."
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer",
                    content=customer_3, turn_index=turn
                )

                conversation.ordered_dishes = [{"raw_order_text": customer_3}]
                conversation.save(update_fields=["ordered_dishes"])

                if pause > 0:
                    time.sleep(pause)

            except Exception as e:
                self.stderr.write(f"[ERROR] Failed conversation for {cust_code}: {e!r}")
                continue

        self.stdout.write(self.style.SUCCESS(f"Simulated {n} conversations."))