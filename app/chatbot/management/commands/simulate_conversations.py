import os
import json
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
    "Additionally, label yourself internally as one of: omnivore, vegetarian, vegan.\n"
    "If vegetarian: no meat/fish. If vegan: no animal products.\n"
    "When asked what you want to order, choose dish(es) consistent with your dietary label.\n"
    "Return answers in plain natural language, no JSON."
)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


# -----------------------------------------------------------------------------
# Dietary label sampling (controlled distribution)
# -----------------------------------------------------------------------------
def sample_label(rng: random.Random) -> str:
    """
    Sample a dietary label with a controlled distribution to guarantee that
    vegetarian/vegan customers occur in the simulated dataset.
    """
    r = rng.random()
    if r < 0.02:
        return "vegan"
    if r < 0.2:
        return "vegetarian"
    return "omnivore"


# -----------------------------------------------------------------------------
# Prompt injection to force a STRICT foods format
# -----------------------------------------------------------------------------
def inject_customer_system_with_label(base_system: str, label: str) -> str:
    """
    Enforce a strict, machine-parseable format ONLY for the "top 3 favourite foods" answer.

    Required format:
      FOOD_1, FOOD_2, FOOD_3
      DIETARY_LABEL=<label>

    All other turns: natural language is allowed.
    """
    return (
        base_system
        + "\n\n"
        + "IMPORTANT CONSTRAINTS (STRICT):\n"
        + f"- You are strictly '{label}'.\n"
        + "- When asked about your top 3 favourite foods, you MUST reply in EXACTLY this format:\n"
        + "  FOOD_1, FOOD_2, FOOD_3\n"
        + f"  DIETARY_LABEL={label}\n"
        + "- The first line must contain exactly three foods separated by commas.\n"
        + "- Do NOT add any extra words (no sentences, no politeness, no explanations).\n"
        + "- Do NOT include ordering wishes in that answer.\n"
        + "- For all other questions, answer normally in plain natural language.\n"
    )


def parse_favorite_foods_strict(customer_answer: str) -> Tuple[List[str], str]:
    """
    Parse the STRICT format:
      FOOD_1, FOOD_2, FOOD_3
      DIETARY_LABEL=<label>

    Raises ValueError on non-conforming answers.
    """
    text = (customer_answer or "").strip()

    m = re.search(
        r"\bDIETARY_LABEL\s*=\s*(omnivore|vegetarian|vegan)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        raise ValueError("Missing DIETARY_LABEL tag.")
    label = m.group(1).lower()

    before = text[: m.start()].strip()
    lines = [ln.strip() for ln in before.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Missing foods line before DIETARY_LABEL.")
    foods_line = lines[0]

    parts = [p.strip(" \t\r\n.,;:!?'\"") for p in foods_line.split(",")]
    parts = [p for p in parts if p]
    if len(parts) != 3:
        raise ValueError(f"Expected exactly 3 comma-separated foods, got {len(parts)}.")

    bad_tokens = (" i would ", " i'd ", " order ", " please", " want to ", " like to ","great choice")
    for f in parts:
        low = f.lower()
        if any(t in low for t in bad_tokens):
            raise ValueError("Foods contain sentence-like/order-like text.")
        if len(f) > 40:
            raise ValueError("Food item too long, likely not a food name.")

    if len({p.lower() for p in parts}) != 3:
        raise ValueError("Foods must be three different items.")

    return parts, label


# -----------------------------------------------------------------------------
# OpenAI call wrapper
# -----------------------------------------------------------------------------
def call_agent(client: OpenAI, system: str, transcript: List[Dict]) -> str:
    conv_text = ""
    for m in transcript:
        conv_text += f"{m['role'].upper()}: {m['content']}\n"

    prompt = (
        f"{system}\n\n"
        f"Conversation so far:\n{conv_text}\n\n"
        "Your next reply:"
    )

    resp = client.responses.create(model=MODEL, input=prompt)
    return (resp.output_text or "").strip()


# -----------------------------------------------------------------------------
# Robust extraction fallback (kept as a safety net)
# -----------------------------------------------------------------------------
def extract_foods_and_label(client: OpenAI, customer_answer: str) -> Tuple[List[str], str]:
    """
    Robust extraction of:
      - dietary_label ∈ {omnivore, vegetarian, vegan}
      - favorite_foods: exactly 3 strings
    Strategy:
      (0) deterministic tag extraction from free text: DIETARY_LABEL=<label>
      (A) up to 3 LLM attempts (JSON + repair)
      (B) if still failing: deterministic local fallback parsing from free text
    """

    m = re.search(
        r"\bDIETARY_LABEL\s*=\s*(omnivore|vegetarian|vegan)\b",
        customer_answer or "",
        flags=re.IGNORECASE,
    )
    tag_label = m.group(1).lower() if m else None

    def _try_parse_json(text: str) -> dict:
        text = (text or "").strip()

        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1 :]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].strip()

        try:
            return json.loads(text)
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])

        raise ValueError("No JSON object found")

    def _normalize_label(label: str) -> str:
        label = (label or "").strip().lower()
        if label in ("omnivore", "vegetarian", "vegan"):
            return label
        return "omnivore"

    def _validate(data: dict) -> Tuple[List[str], str]:
        foods = data.get("favorite_foods", [])
        raw_label = tag_label or data.get("dietary_label", "omnivore")
        label = _normalize_label(raw_label)

        if not isinstance(foods, list):
            raise ValueError("favorite_foods must be a list")

        foods = [str(x).strip() for x in foods if str(x).strip()]
        if len(foods) != 3:
            raise ValueError("favorite_foods must contain exactly three items")

        return foods, label

    def _fallback_from_text(answer: str) -> Tuple[List[str], str]:
        a = (answer or "").strip()
        low = a.lower()

        if tag_label in ("omnivore", "vegetarian", "vegan"):
            label = tag_label
        else:
            if "vegan" in low:
                label = "vegan"
            elif "vegetarian" in low or "vegetar" in low:
                label = "vegetarian"
            else:
                label = "omnivore"

        for token in [" are ", " zijn ", ":", "-"]:
            if token in low:
                idx = low.find(token)
                cand = a[idx + len(token) :].strip()
                if len(cand) >= 5:
                    a = cand
                    break

        a = re.sub(
            r"\bDIETARY_LABEL\s*=\s*(omnivore|vegetarian|vegan)\b",
            "",
            a,
            flags=re.IGNORECASE,
        ).strip()

        a_norm = a.replace(" and ", ", ").replace(" en ", ", ")
        parts = [p.strip(" .;!?\n\t\"'") for p in a_norm.split(",")]
        parts = [p for p in parts if p]

        if len(parts) < 3:
            a_norm2 = a_norm.replace(" & ", ", ").replace("/", ", ")
            parts2 = [p.strip(" .;!?\n\t\"'") for p in a_norm2.split(",")]
            parts2 = [p for p in parts2 if p]
            parts = parts2

        foods = parts[:3]
        if len(foods) < 3:
            fillers = ["pizza", "pasta", "salad"]
            for f in fillers:
                if len(foods) >= 3:
                    break
                if f not in foods:
                    foods.append(f)

        return foods, label

    schema = (
        "{\n"
        '  "dietary_label": "omnivore|vegetarian|vegan",\n'
        '  "favorite_foods": ["food1","food2","food3"]\n'
        "}"
    )

    prompt_base = (
        "You are an information extraction system.\n"
        "Return ONLY a valid JSON object. No markdown. No code fences. No extra text.\n"
        f"Schema (exact):\n{schema}\n\n"
        "Rules:\n"
        "- favorite_foods must have exactly 3 items.\n"
        "- dietary_label must be exactly one of: omnivore, vegetarian, vegan.\n\n"
        f"Customer answer:\n{customer_answer}\n"
    )

    last_raw = ""
    for attempt in range(1, 4):
        if attempt == 1:
            prompt = prompt_base
        else:
            prompt = (
                "Your previous output was invalid.\n"
                "Return ONLY valid JSON exactly matching the schema.\n\n"
                f"Schema:\n{schema}\n\n"
                f"Invalid output:\n{last_raw}\n\n"
                f"Customer answer:\n{customer_answer}\n"
            )

        resp = client.responses.create(model=MODEL, input=prompt)
        raw = (resp.output_text or "").strip()
        last_raw = raw

        try:
            data = _try_parse_json(raw)
            return _validate(data)
        except Exception:
            continue

    return _fallback_from_text(customer_answer)


# -----------------------------------------------------------------------------
# Data hygiene helpers
# -----------------------------------------------------------------------------
def _dedupe_and_fill_three(foods: List[str]) -> List[str]:
    """
    Ensure exactly 3 non-empty, unique foods (case-insensitive). If fewer than 3
    after deduplication, fill deterministically from a small fallback list.
    """
    seen = set()
    out: List[str] = []
    for f in foods or []:
        s = str(f).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) == 3:
            return out

    fillers = ["pizza", "pasta", "salad", "sushi", "burger", "tacos", "ramen", "curry"]
    for f in fillers:
        if len(out) == 3:
            break
        if f.lower() not in seen:
            out.append(f)
            seen.add(f.lower())

    return out[:3]


def _looks_like_order_text(s: str) -> bool:
    """
    Heuristic: detect order-like sentences rather than a foods list.
    """
    t = (s or "").lower()
    patterns = [
        r"\bi would like to order\b",
        r"\bcan i (get|have|order)\b",
        r"\b(i('| a)m|im) going to have\b",
        r"\border\b",
        r"\bdish(es)?\b",
        r"\bfor (my|our) (main|starter|dessert)\b",
    ]
    return any(re.search(p, t) for p in patterns)


def _force_foods_answer(client: OpenAI, customer_system: str, transcript: List[Dict], target_label: str) -> str:
    """
    One repair turn: force a strict foods-only answer to avoid parsing pollution.
    """
    repair_system = (
        customer_system
        + "\n\n"
        + "REPAIR MODE (strict):\n"
          "- Your next reply MUST contain ONLY your top 3 favourite foods.\n"
          "- Format exactly:\n"
          "  FOOD_1, FOOD_2, FOOD_3\n"
          f"  DIETARY_LABEL={target_label}\n"
          "- Do NOT mention ordering, dishes, restaurants, or any extra words.\n"
    )
    return call_agent(client, repair_system, transcript)


# -----------------------------------------------------------------------------
# Indexing helper to avoid overwriting customers across batches
# -----------------------------------------------------------------------------
def _next_customer_index() -> int:
    """
    Compute the next available integer index for customer_code CUST_###.

    We scan existing codes and take max numeric suffix. This avoids overwriting
    CUST_001.. when the command is executed repeatedly in small batches.
    """
    codes = SimulatedCustomer.objects.values_list("customer_code", flat=True)

    max_idx = 0
    for code in codes:
        if not code:
            continue
        m = re.match(r"^CUST_(\d+)$", code)
        if m:
            try:
                idx = int(m.group(1))
                if idx > max_idx:
                    max_idx = idx
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

        # Determine start index BEFORE initializing RNG
        start_idx = start_arg if start_arg and start_arg > 0 else _next_customer_index()
        end_idx = start_idx + n - 1

        # Shift RNG per batch to avoid repeating label patterns in small batches
        rng = random.Random(seed + start_idx)

        self.stdout.write(f"Simulating customers {start_idx}..{end_idx} (n={n}).")

        for i in range(start_idx, end_idx + 1):
            cust_code = f"CUST_{i:03d}"

            try:
                customer, _ = SimulatedCustomer.objects.get_or_create(customer_code=cust_code)

                # Predetermine dietary label to ensure non-empty veg/vegan cohort
                target_label = sample_label(rng)
                customer_system = inject_customer_system_with_label(CUSTOMER_SYSTEM, target_label)

                conversation = SimulatedConversation.objects.create(customer=customer)

                transcript: List[Dict] = []
                turn = 1

                # Turn 1: waiter
                waiter_1 = call_agent(client, WAITER_SYSTEM, transcript)
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_1, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_1})
                turn += 1

                # Turn 2: customer day summary
                customer_1 = call_agent(client, customer_system, transcript)
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer", content=customer_1, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_1})
                customer.day_summary = customer_1[:5000]
                turn += 1

                # Turn 3: waiter asks foods
                waiter_2 = call_agent(client, WAITER_SYSTEM, transcript)
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_2, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_2})
                turn += 1

                # Turn 4: customer answers foods (STRICT expected)
                customer_2 = call_agent(client, customer_system, transcript)
                SimulatedMessage.objects.create(
                    conversation=conversation, role="customer", content=customer_2, turn_index=turn
                )
                transcript.append({"role": "user", "content": customer_2})
                turn += 1

                # Prefer strict parsing; if it fails, do one repair turn; then fallback extraction.
                foods: List[str]
                try:
                    foods, parsed_label = parse_favorite_foods_strict(customer_2)
                except Exception:
                    # If the model accidentally answered with an order-like sentence or extra text: repair once.
                    if _looks_like_order_text(customer_2):
                        self.stderr.write(f"[WARN] {cust_code}: foods answer looked like an order; forcing repair.")
                    else:
                        self.stderr.write(f"[WARN] {cust_code}: strict parse failed; forcing repair.")

                    customer_2b = _force_foods_answer(client, customer_system, transcript, target_label)
                    SimulatedMessage.objects.create(
                        conversation=conversation, role="customer", content=customer_2b, turn_index=turn
                    )
                    transcript.append({"role": "user", "content": customer_2b})
                    turn += 1

                    try:
                        foods, parsed_label = parse_favorite_foods_strict(customer_2b)
                    except Exception:
                        foods, parsed_label = extract_foods_and_label(client, customer_2b)
                        foods = _dedupe_and_fill_three(foods)
                    else:
                        foods = _dedupe_and_fill_three(foods)
                else:
                    foods = _dedupe_and_fill_three(foods)

                # Enforce predetermined label (avoid drift; guarantees cohort distribution)
                customer.favorite_foods = foods
                customer.dietary_label = target_label
                customer.save(update_fields=["day_summary", "favorite_foods", "dietary_label"])

                # Turn 5: waiter asks order
                waiter_3 = call_agent(client, WAITER_SYSTEM, transcript)
                SimulatedMessage.objects.create(
                    conversation=conversation, role="waiter", content=waiter_3, turn_index=turn
                )
                transcript.append({"role": "assistant", "content": waiter_3})
                turn += 1

                # Turn 6: customer orders
                customer_3 = call_agent(client, customer_system, transcript)
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