import json
from typing import Any, Dict, List

from openai import OpenAI

from .config import MODEL


def call_agent(
    client: OpenAI,
    system: str,
    transcript: List[Dict[str, str]],
    turn_instruction: str,
) -> str:
    """Conversational turn using proper message roles."""
    messages = [{"role": "system", "content": system}]

    for m in transcript:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": turn_instruction})

    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.7,
    )

    if not resp.choices:
        raise ValueError("Model returned no choices")

    return (resp.choices[0].message.content or "").strip()


def extract_structured(
    client: OpenAI,
    system: str,
    user_prompt: str,
    json_schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Schema-constrained JSON extraction."""
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": json_schema,
        },
    )

    if not resp.choices:
        raise ValueError("Model returned no choices")

    content = resp.choices[0].message.content or "{}"

    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    return json.loads(content)


def chat_completion_json_schema(
    client: OpenAI,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_schema: Dict[str, Any],
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """Schema-constrained completion with explicit model parameter."""
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": json_schema,
        },
    )

    if not resp.choices:
        raise ValueError("Model returned no choices")

    content = resp.choices[0].message.content or "{}"

    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    try:
        return json.loads(content)
    except Exception as e:
        raise ValueError(f"Model returned non-JSON content: {content[:500]}") from e
