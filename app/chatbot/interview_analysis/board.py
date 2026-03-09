from typing import Any, Dict

from openai import OpenAI

from .config import MODEL
from .openai_helpers import chat_completion_json_schema
from .schemas import BOARD_RECOMMENDATION_JSON_SCHEMA


def synthesize_board_recommendation(
    client: OpenAI,
    *,
    top_theme: Dict[str, Any],
    n_interviews: int,
) -> Dict[str, Any]:
    evidence_lines = []
    for ex in top_theme["examples"][:5]:
        evidence_lines.append(
            f"- Issue: {ex['issue']} | Impact: {ex['impact']} | Evidence: \"{ex['evidence_quote']}\""
        )

    system_prompt = (
        "You are a senior strategy consultant summarizing internal employee interviews. "
        "Return only valid JSON matching the provided schema."
    )

    user_prompt = (
        "Produce the SINGLE most important improvement initiative to share with the Board.\n"
        "Requirements:\n"
        "- initiative_statement must be exactly one sentence\n"
        "- evidence_bullets must contain 3 to 5 concise bullets grounded in the evidence below\n"
        "- kpis must contain exactly 3 KPIs\n"
        "- provide a concrete 30/60/90-day action plan\n"
        "- be specific and operational; avoid generic advice\n\n"
        f"TOP THEME: {top_theme['theme_label']}\n"
        f"{top_theme.get('theme_summary', '')}\n\n"
        f"SCALE:\n"
        f"- Raised in {top_theme['frequency_interviews']} of {n_interviews} interviews "
        f"({top_theme['frequency_ratio']:.0%})\n"
        f"- {top_theme['n_issue_records']} distinct issue mentions\n"
        f"- Average severity: {top_theme['avg_severity']:.1f} / 5\n"
        f"- Spans {top_theme.get('department_spread', 0)} departments: "
        f"{', '.join(top_theme.get('departments', [])) or 'n/a'}\n\n"
        f"REPRESENTATIVE ISSUE: {top_theme.get('medoid_issue', '')}\n"
        f"ROOT CAUSE: {top_theme.get('medoid_root_cause', '')}\n\n"
        f"EMPLOYEE EVIDENCE:\n" + "\n".join(evidence_lines) + "\n"
    )

    parsed = chat_completion_json_schema(
        client,
        model=MODEL,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=BOARD_RECOMMENDATION_JSON_SCHEMA,
        temperature=0.0,
    )

    return {
        "initiative_statement": str(parsed.get("initiative_statement") or "").strip(),
        "evidence_bullets": [
            str(x).strip() for x in (parsed.get("evidence_bullets") or []) if str(x).strip()
        ][:5],
        "kpis": [
            {
                "name": str(k.get("name") or "").strip(),
                "definition": str(k.get("definition") or "").strip(),
                "target_direction": str(k.get("target_direction") or "").strip(),
            }
            for k in (parsed.get("kpis") or [])
            if isinstance(k, dict)
        ][:3],
        "action_plan_30_60_90": {
            "day_30": [str(x).strip() for x in ((parsed.get("action_plan_30_60_90") or {}).get("day_30") or []) if str(x).strip()][:5],
            "day_60": [str(x).strip() for x in ((parsed.get("action_plan_30_60_90") or {}).get("day_60") or []) if str(x).strip()][:5],
            "day_90": [str(x).strip() for x in ((parsed.get("action_plan_30_60_90") or {}).get("day_90") or []) if str(x).strip()][:5],
        },
    }


def default_board_fallback(top_theme: Dict[str, Any], n_interviews: int) -> Dict[str, Any]:
    return {
        "initiative_statement": (
            f"Prioritise a targeted improvement initiative for '{top_theme['theme_label']}' "
            f"to address the most recurrent and severe cross-interview operational issue."
        ),
        "evidence_bullets": [
            f"Theme appears in {top_theme['frequency_interviews']} of {n_interviews} interviews.",
            f"Average severity is {top_theme['avg_severity']}.",
            f"Department spread is {top_theme['department_spread']} departments.",
        ],
        "kpis": [
            {"name": "Issue recurrence rate", "definition": "Share of interviews in which this theme recurs.", "target_direction": "decrease"},
            {"name": "Average impact severity", "definition": "Mean severity score of issues mapped to this theme.", "target_direction": "decrease"},
            {"name": "Action completion rate", "definition": "Share of agreed mitigation actions completed on time.", "target_direction": "increase"},
        ],
        "action_plan_30_60_90": {
            "day_30": ["Confirm ownership and scope for the top theme.", "Validate root causes with affected departments."],
            "day_60": ["Implement initial corrective actions and operating controls.", "Introduce KPI tracking and governance review cadence."],
            "day_90": ["Assess measurable impact against baseline.", "Standardise the successful intervention across departments."],
        },
    }
