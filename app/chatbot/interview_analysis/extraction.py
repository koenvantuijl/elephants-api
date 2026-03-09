import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from openai import OpenAI

from chatbot.models import SimulatedInterview, SimulatedInterviewMessage

from .config import MODEL
from .openai_helpers import chat_completion_json_schema
from .schemas import EXTRACTION_JSON_SCHEMA


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------
@dataclass
class IssueRecord:
    interview_id: int
    employee_code: str
    department: str
    role_title: str
    issue: str
    impact: str
    root_cause: str
    suggested_action: str
    confidence: float       # 0..1
    severity: float         # 1..5
    severity_source: str    # "llm" or "regex_fallback"
    evidence_quote: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_float(x: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.5) -> float:
    """Clamp *x* to [lo, hi], returning *default* on any failure."""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return max(lo, min(hi, v))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------
def severity_from_impact_text(impact_text: str) -> float:
    """
    Legacy regex heuristic — used ONLY when the extraction payload does not
    contain an LLM-provided severity score (i.e. older interviews).

    Returns a value in [1.0, 5.0].  The baseline for unrecognised text is 2.5
    (conservative middle).
    """
    t = (impact_text or "").lower()
    if not t.strip():
        return 2.5

    operational = 0.0
    financial = 0.0
    customer = 0.0
    risk = 0.0

    if re.search(
        r"\b(outage|downtime|incident|critical|sev[ -]?\d|production stop|stalls?|failure|overheated)\b", t
    ):
        operational = max(operational, 4.5)
    elif re.search(r"\b(delay|slowdown|bottleneck|rework|inefficienc|waiting|backlog)\w*\b", t):
        operational = max(operational, 3.5)

    if re.search(r"\$|€|£|\b\d+\s*(k|m)\b|\brevenue\b|\bcost\b|\bbudget\b", t):
        financial = max(financial, 4.2)

    if re.search(
        r"\b(customer|customers|complaint|complaints|sla|churn|nps|trust|delivery|deliveries|satisfaction)\b", t
    ):
        customer = max(customer, 4.0)

    if re.search(r"\b(risk|breach|compliance|safety|patient|patients|audit|regulatory)\b", t):
        risk = max(risk, 4.3)

    if re.search(r"\b(\d+)\s*(min|minute|minutes|hour|hours|day|days|week|weeks)\b", t):
        operational = max(operational, 4.0)

    if operational == 0.0 and financial == 0.0 and customer == 0.0 and risk == 0.0:
        return 2.5

    sev = 0.40 * operational + 0.20 * financial + 0.20 * customer + 0.20 * risk
    return max(1.0, min(5.0, sev))


def resolve_severity(item: Dict[str, Any]) -> Tuple[float, str]:
    """Return (severity_value, source). Prefer LLM; fall back to regex."""
    raw = item.get("severity")
    if raw is not None:
        try:
            v = float(raw)
            if 1.0 <= v <= 5.0:
                return v, "llm"
        except (TypeError, ValueError):
            pass
    return severity_from_impact_text(item.get("impact") or ""), "regex_fallback"


# ---------------------------------------------------------------------------
# Validation / parsing
# ---------------------------------------------------------------------------
def validate_extraction_object(parsed: Any) -> Dict[str, Any]:
    if not isinstance(parsed, dict):
        return {"opportunities": []}

    opps = parsed.get("opportunities")
    if not isinstance(opps, list):
        return {"opportunities": []}

    out: List[Dict[str, Any]] = []
    for it in opps:
        if not isinstance(it, dict):
            continue

        severity_val, severity_src = resolve_severity(it)

        item = {
            "issue": str(it.get("issue") or "").strip(),
            "impact": str(it.get("impact") or "").strip(),
            "root_cause": str(it.get("root_cause") or "").strip(),
            "suggested_action": str(it.get("suggested_action") or "").strip(),
            "confidence": safe_float(it.get("confidence"), 0.0, 1.0, 0.5),
            "severity": severity_val,
            "severity_source": severity_src,
        }

        if item["issue"]:
            out.append(item)

    return {"opportunities": out}


def validate_opportunities(items: Any) -> List[Dict[str, Any]]:
    """Validate and normalise a list of opportunity dicts (simulation side)."""
    if not isinstance(items, list):
        return []

    valid = []
    for it in items:
        if not isinstance(it, dict):
            continue

        issue = str(it.get("issue") or "").strip()
        if not issue:
            continue

        valid.append({
            "issue": issue,
            "impact": str(it.get("impact") or "").strip(),
            "root_cause": str(it.get("root_cause") or "").strip(),
            "suggested_action": str(it.get("suggested_action") or "").strip(),
            "confidence": safe_float(it.get("confidence"), 0.0, 1.0, 0.5),
            "severity": safe_float(it.get("severity"), 1.0, 5.0, 2.5),
        })

    return valid


def is_canonical_extraction_payload(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if not isinstance(raw.get("opportunities"), list):
        return False
    return True


def parse_extraction(iv: SimulatedInterview) -> Dict[str, Any]:
    raw = iv.improvement_opportunities
    if not is_canonical_extraction_payload(raw):
        return {"opportunities": []}
    return validate_extraction_object(raw)


def canonicalize_and_persist_extraction(
    client: OpenAI,
    iv: SimulatedInterview,
    *,
    regen_missing: bool = False,
) -> Dict[str, Any]:
    ex = parse_extraction(iv)

    if ex.get("opportunities"):
        return ex

    if regen_missing:
        ex = regenerate_extraction(client, iv.id)
        ex = validate_extraction_object(ex)
        SimulatedInterview.objects.filter(id=iv.id).update(improvement_opportunities=ex)
        return ex

    return ex


# ---------------------------------------------------------------------------
# Re-extraction for legacy interviews
# ---------------------------------------------------------------------------
def regenerate_extraction(client: OpenAI, interview_id: int) -> Dict[str, Any]:
    msgs = (
        SimulatedInterviewMessage.objects
        .filter(interview_id=interview_id)
        .order_by("turn_index")
        .only("role", "content")
    )
    transcript = "\n".join([f"{m.role.upper()}: {m.content}" for m in msgs])

    system_prompt = (
        "You are analyzing an internal business interview transcript. "
        "Identify improvement opportunities grounded only in the employee statements. "
        "Return only valid JSON matching the provided schema."
    )

    user_prompt = (
        "Extract the most important business improvement opportunities from this interview.\n"
        "Rules:\n"
        "- Be concise and specific\n"
        "- Ground every opportunity in the transcript\n"
        "- confidence must be between 0 and 1\n"
        "- severity must be between 1 and 5:\n"
        "    1 = minor annoyance, 2 = team-level friction, 3 = significant workflow/customer problem,\n"
        "    4 = serious financial/safety/operational issue, 5 = critical business risk\n"
        "- Do not invent facts not present in the transcript\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )

    try:
        parsed = chat_completion_json_schema(
            client,
            model=MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=EXTRACTION_JSON_SCHEMA,
            temperature=0.0,
        )
        return validate_extraction_object(parsed)
    except Exception:
        return {"opportunities": []}
