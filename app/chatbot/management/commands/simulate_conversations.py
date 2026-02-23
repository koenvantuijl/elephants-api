import os
import time
import random
import re
from typing import Tuple, List, Dict

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
# Dietary label sampling (controlled distribution)
# -----------------------------------------------------------------------------
def sample_label(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.02:
        return "vegan"
    if r < 0.2:
        return "vegetarian"
    return "omnivore"


# -----------------------------------------------------------------------------
# Keep label fixed, keep conversation natural (no strict foods format)
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
# Parse three foods from natural language (simple heuristic)
# -----------------------------------------------------------------------------
def parse_three_foods_natural(text: str) -> List[str]:
    """
    Extract exactly 3 foods from a natural-language answer.
    More robust against sentences like:
      "Sure! I really love pasta with tomato sauce, ..."
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty foods answer")

    # -------------------------------------------------
    # 1. Remove common leading phrases
    # -------------------------------------------------
    s = re.sub(
        r"^(sure!?|yeah!?|yes!?|of course!?|well,?|honestly,?)\s*",
        "",
        s,
        flags=re.I,
    )

    s = re.sub(
        r"^(i (really )?(love|like|enjoy|would say|d say|usually like))\s+",
        "",
        s,
        flags=re.I,
    )

    s = re.sub(
        r"^(my (top )?(three )?(favorite|favourite) foods (are|would be))\s+",
        "",
        s,
        flags=re.I,
    )

    # -------------------------------------------------
    # 2. Normalize separators
    # -------------------------------------------------
    s_norm = (
        s.replace(" and ", ", ")
        .replace(" & ", ", ")
        .replace(";", ", ")
        .replace("/", ", ")
        .replace("\n", ", ")
    )

    parts = [p.strip(" \t\r\n.,;:!?'\"") for p in s_norm.split(",")]
    parts = [p for p in parts if p]

    # -------------------------------------------------
    # 3. Clean each candidate
    # -------------------------------------------------
    cleaned: List[str] = []
    seen = set()

    for p in parts:
        item = p.strip()

        # remove trailing sentence fragments
        item = re.sub(r"\b(they|which|that)\b.*$", "", item, flags=re.I).strip()

        # remove leading filler words again (safety)
        item = re.sub(
            r"^(i (really )?(love|like|enjoy))\s+",
            "",
            item,
            flags=re.I,
        )

        # length guard (important!)
        if len(item) > 40:
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
# OpenAI call wrapper (simple per-turn instruction line)
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
# Indexing helper to avoid overwriting customers across batches
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


class Command(BaseCommand):
    help = "Simulate waiter-customer conversations and store them in the database."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=100)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument(
            "--start",
            type=int,
            default=0,
            help=(
                "Optional explicit start index for customer codes (CUST_<start>...). "
                "If 0, auto-detect next free index."
            ),
        )

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

        start_idx = start_arg if start_arg and start_arg > 0 else _next_customer_index()
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

                # Turn 1: waiter -> day question
                waiter_1 = call_agent(
                    client,
                    WAITER_SYSTEM,
                    transcript,
                    turn_instruction="This turn: step 1 only (welcome + ask about the day). Ask one question.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_1, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_1})
                turn += 1

                # Turn 2: customer -> day answer
                customer_1 = call_agent(
                    client,
                    customer_system,
                    transcript,
                    turn_instruction="Answer naturally in 1-2 sentences about your day.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer", content=customer_1, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_1})
                customer.day_summary = customer_1[:5000]
                turn += 1

                # Turn 3: waiter -> foods question
                waiter_2 = call_agent(
                    client,
                    WAITER_SYSTEM,
                    transcript,
                    turn_instruction="This turn: step 2 only (ask top 3 favourite foods). Ask one question.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_2, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_2})
                turn += 1

                # Turn 4: customer -> foods answer (natural)
                customer_2 = call_agent(
                    client,
                    customer_system,
                    transcript,
                    turn_instruction="Answer with your top 3 favourite foods (three items) in a natural way.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer", content=customer_2, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_2})
                turn += 1

                foods = parse_three_foods_natural(customer_2)
                customer.favorite_foods = foods
                customer.dietary_label = target_label
                customer.save(update_fields=["day_summary", "favorite_foods", "dietary_label"])

                # Turn 5: waiter -> order question
                waiter_3 = call_agent(
                    client,
                    WAITER_SYSTEM,
                    transcript,
                    turn_instruction="This turn: step 3 only (ask what they want to order). Ask one question.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_3, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_3})
                turn += 1

                # Turn 6: customer -> order answer
                customer_3 = call_agent(
                    client,
                    customer_system,
                    transcript,
                    turn_instruction="Answer what you would order today, consistent with your dietary label.",
                )
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer", content=customer_3, turn_index=turn
                )

                conversation.ordered_dishes = [{"raw_order_text": customer_3}]
                conversation.save(update_fields=["ordered_dishes"])

                if pause > 0:
                    time.sleep(pause)

            except Exception as e:
                self.stderr.write(f"[ERROR] Failed conversation for {cust_code}: {e!r}")
                continue

        self.stdout.write(self.style.SUCCESS(f"Simulated {n} conversations."))