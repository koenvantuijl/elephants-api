from typing import Any, Dict

# ---------------------------------------------------------------------------
# Extraction schema — used by both simulate + analyze commands
# ---------------------------------------------------------------------------
EXTRACTION_JSON_SCHEMA: Dict[str, Any] = {
    "name": "interview_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "opportunities": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "issue": {"type": "string"},
                        "impact": {"type": "string"},
                        "root_cause": {"type": "string"},
                        "suggested_action": {"type": "string"},
                        "confidence": {"type": "number"},
                        "severity": {
                            "type": "number",
                            "description": (
                                "Severity of the issue on a 1-5 scale. "
                                "1 = minor annoyance with limited scope, "
                                "2 = noticeable friction affecting a team, "
                                "3 = significant problem affecting workflows or customers, "
                                "4 = serious issue with financial, safety, or major operational consequences, "
                                "5 = critical risk to the business (compliance, revenue, safety)."
                            ),
                        },
                    },
                    "required": [
                        "issue",
                        "impact",
                        "root_cause",
                        "suggested_action",
                        "confidence",
                        "severity",
                    ],
                },
            },
        },
        "required": ["opportunities"],
    },
    "strict": True,
}

# ---------------------------------------------------------------------------
# Theme label schema — used by analysis
# ---------------------------------------------------------------------------
THEME_LABEL_JSON_SCHEMA: Dict[str, Any] = {
    "name": "theme_label",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "theme_label": {
                "type": "string",
                "description": "A concise business theme title of at most 8 words.",
            },
            "theme_summary": {
                "type": "string",
                "description": "One sentence summarising the cluster.",
            },
        },
        "required": ["theme_label", "theme_summary"],
    },
    "strict": True,
}

# ---------------------------------------------------------------------------
# Board recommendation schema — used by analysis
# ---------------------------------------------------------------------------
BOARD_RECOMMENDATION_JSON_SCHEMA: Dict[str, Any] = {
    "name": "board_recommendation",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "initiative_statement": {
                "type": "string",
                "description": "Single-sentence board-level improvement initiative.",
            },
            "evidence_bullets": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {"type": "string"},
            },
            "kpis": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "definition": {"type": "string"},
                        "target_direction": {
                            "type": "string",
                            "enum": ["increase", "decrease", "maintain"],
                        },
                    },
                    "required": ["name", "definition", "target_direction"],
                },
            },
            "action_plan_30_60_90": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "day_30": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {"type": "string"},
                    },
                    "day_60": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {"type": "string"},
                    },
                    "day_90": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {"type": "string"},
                    },
                },
                "required": ["day_30", "day_60", "day_90"],
            },
        },
        "required": [
            "initiative_statement",
            "evidence_bullets",
            "kpis",
            "action_plan_30_60_90",
        ],
    },
    "strict": True,
}
