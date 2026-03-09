import os
import random
import re
import time
from typing import Any, Dict, List, Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from openai import OpenAI

from chatbot.models import (
    SimulatedEmployee,
    SimulatedInterview,
    SimulatedInterviewMessage,
)

from chatbot.interview_analysis.config import MODEL
from chatbot.interview_analysis.schemas import EXTRACTION_JSON_SCHEMA
from chatbot.interview_analysis.prompts import EXTRACTION_SYSTEM, build_employee_system, build_interviewer_system
from chatbot.interview_analysis.openai_helpers import call_agent, extract_structured
from chatbot.interview_analysis.sampling import sample_company_context, sample_employee_persona
from chatbot.interview_analysis.extraction import validate_opportunities


# -----------------------------------------------------------------------------
# Seed resolution
# -----------------------------------------------------------------------------
def resolve_seed(cli_seed: Optional[int]) -> int:
    if cli_seed is not None:
        return int(cli_seed)

    env_seed = os.environ.get("SIMULATION_SEED")
    if env_seed not in (None, ""):
        try:
            return int(env_seed)
        except ValueError as e:
            raise ValueError("SIMULATION_SEED must be an integer") from e

    return 42


def _next_employee_index() -> int:
    codes = SimulatedEmployee.objects.values_list("employee_code", flat=True)
    max_idx = 0
    for code in codes:
        if not code:
            continue
        m = re.match(r"^EMP_(\d+)$", code)
        if m:
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except Exception:
                continue
    return max_idx + 1


def interviewer_question_ok(text: str) -> bool:
    return bool(text and text.strip().endswith("?"))


def build_transcript_text(transcript: List[Dict[str, str]]) -> str:
    lines = []
    for m in transcript:
        role = m["role"]
        if role == "assistant":
            label = "INTERVIEWER"
        elif role == "user":
            label = "EMPLOYEE"
        else:
            label = role.upper()
        lines.append(f"{label}: {m['content']}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Command
# -----------------------------------------------------------------------------
class Command(BaseCommand):
    help = "Simulate interviewer-employee conversations and store them in the database."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=100)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--seed", type=int, default=None)
        parser.add_argument("--start", type=int, default=0)
        parser.add_argument("--minq", type=int, default=5)
        parser.add_argument("--maxq", type=int, default=10)

    def handle(self, *args, **opts):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set (set it in your docker compose env/.env).")

        client = OpenAI(api_key=api_key)

        n = int(opts["n"])
        pause = float(opts["sleep"])
        seed = resolve_seed(opts.get("seed"))
        start_arg = int(opts["start"])
        minq = int(opts["minq"])
        maxq = int(opts["maxq"])

        if minq < 1 or maxq < minq:
            raise ValueError("Invalid minq/maxq. Require 1 <= minq <= maxq.")

        start_idx = start_arg if start_arg > 0 else _next_employee_index()
        end_idx = start_idx + n - 1

        self.stdout.write(f"Using simulation seed: {seed}")

        rng_company = random.Random(seed)
        company_context = sample_company_context(rng_company)
        interviewer_system = build_interviewer_system(company_context)

        self.stdout.write(
            f"Simulating interviews for employees {start_idx}..{end_idx} (n={n}), "
            f"questions per interview: [{minq},{maxq}], model={MODEL}."
        )

        stats = {"succeeded": 0, "failed": 0, "extractions_valid": 0, "extractions_failed": 0, "total_opportunities": 0}

        for i in range(start_idx, end_idx + 1):
            emp_code = f"EMP_{i:03d}"

            try:
                with transaction.atomic():
                    rng_i = random.Random(seed + i)

                    persona = sample_employee_persona(rng_i)
                    employee, _ = SimulatedEmployee.objects.get_or_create(employee_code=emp_code)
                    employee.department = persona["department"]
                    employee.role_title = persona["role_title"]
                    employee.seniority = persona["seniority"]
                    employee.save(update_fields=["department", "role_title", "seniority"])

                    q_target = rng_i.randint(minq, maxq)

                    interview = SimulatedInterview.objects.create(
                        employee=employee, company_context=company_context, question_target=q_target,
                    )

                    employee_system = build_employee_system(company_context, persona, employee.persona_notes)

                    transcript: List[Dict[str, str]] = []
                    employee_answers: List[str] = []
                    turn = 1

                    # Opening question
                    opening_q = call_agent(client, interviewer_system, transcript, (
                        "Open the interview with EXACTLY ONE question.\n"
                        "Make it broad and exploratory: ask the employee to describe the biggest challenge or frustration "
                        "in their work or the company right now, in their own words.\n"
                        "Do NOT suggest categories or examples.\n"
                        "End with a single '?' and ask exactly one question."
                    ))
                    if not interviewer_question_ok(opening_q):
                        opening_q = call_agent(client, interviewer_system, transcript,
                            "Rewrite as EXACTLY ONE single-sentence question ending with '?'. Do NOT add examples or categories.")

                    SimulatedInterviewMessage.objects.create(interview=interview, role="interviewer", content=opening_q, turn_index=turn)
                    transcript.append({"role": "assistant", "content": opening_q})
                    turn += 1
                    questions_asked = 1

                    did_breadth = False
                    phase = "issue1_concretise"

                    while questions_asked < q_target:
                        # Employee answer
                        emp_a = call_agent(client, employee_system, transcript, (
                            "Answer the interviewer's last question in 2–6 sentences.\n"
                            "Speak as a real employee from your role, team, and seniority level would speak.\n"
                            "Base your answer on what you have personally seen, experienced, or heard first-hand in your work.\n"
                            "Keep the answer natural and conversational; it does not need to be perfectly structured.\n"
                            "Only include metrics if you naturally know them; otherwise describe the impact qualitatively.\n"
                            "It is also okay to be unsure about something, but mention clearly when in doubt.\n"
                            "Do not sound like a consultant or give a structured diagnosis unless the interviewer explicitly asks for that.\n"
                        ))
                        SimulatedInterviewMessage.objects.create(interview=interview, role="employee", content=emp_a, turn_index=turn)
                        transcript.append({"role": "user", "content": emp_a})
                        employee_answers.append(emp_a)
                        turn += 1

                        remaining = q_target - questions_asked

                        # Turn budgeting
                        if remaining <= 1:
                            phase = "prioritise"
                        elif remaining <= 2 and not did_breadth:
                            phase = "breadth"
                        if did_breadth and phase == "breadth" and remaining >= 3:
                            phase = "issue2_optional"

                        # Phase instructions
                        if phase == "issue1_concretise":
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE neutral follow-up question ending with '?'.\n"
                                "You MUST quote or repeat at least one concrete detail from the last answer.\n"
                                "Goal: anchor the topic in ONE specific recent occurrence.\n"
                                "Ask them to walk you through that one occurrence.\n"
                                "Do NOT introduce categories, diagnoses, solutions, or requests for numbers.")
                            phase = "issue1_mechanism"
                        elif phase == "issue1_mechanism":
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE neutral question ending with '?'.\n"
                                "You MUST reference at least one concrete detail from the last answer.\n"
                                "Goal: understand the mechanism — what triggers this and where it begins.\n"
                                "Do NOT propose fixes. Do NOT ask for metrics unless the employee already mentioned numbers.")
                            phase = "issue1_impact"
                        elif phase == "issue1_impact":
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE neutral question ending with '?'.\n"
                                "You MUST reference at least one concrete detail from the last answer.\n"
                                "Goal: clarify practical impact — what gets delayed, degraded, or put at risk.\n"
                                "Do NOT propose fixes.")
                            phase = "breadth"
                        elif phase == "breadth":
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE neutral question ending with '?'.\n"
                                "You MUST reference at least one concrete detail from the last answer.\n"
                                "Goal: ask the employee to share OTHER recurring issues, grounded in examples.\n"
                                "Do NOT propose fixes.")
                            did_breadth = True
                            phase = "prioritise" if remaining <= 2 else "issue2_optional"
                        elif phase == "issue2_optional":
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE neutral question ending with '?'.\n"
                                "You MUST reference at least one concrete detail from the last answer.\n"
                                "Goal: deepen ONE of the additional issues they just mentioned.\n"
                                "Do NOT propose fixes.")
                            phase = "prioritise"
                        else:
                            turn_instruction = (
                                "Based on the employee's last answer, ask EXACTLY ONE prioritisation question ending with '?'.\n"
                                "You MUST reference at least one concrete detail from the last answer.\n"
                                "Ask the employee to name the SINGLE most important improvement priority.\n"
                                "Do NOT offer multiple-choice options and do NOT propose solutions.")

                        next_q = call_agent(client, interviewer_system, transcript, turn_instruction)
                        if not interviewer_question_ok(next_q):
                            next_q = call_agent(client, interviewer_system, transcript,
                                "Rewrite as EXACTLY ONE single-sentence question ending with '?'. "
                                "It must reference a concrete detail from the employee's last answer.")

                        SimulatedInterviewMessage.objects.create(interview=interview, role="interviewer", content=next_q, turn_index=turn)
                        transcript.append({"role": "assistant", "content": next_q})
                        turn += 1
                        questions_asked += 1

                    # Final employee answer
                    emp_final = call_agent(client, employee_system, transcript, (
                        "Answer the interviewer's last question in 2–6 sentences.\n"
                        "Speak as a real employee. Keep the answer natural and conversational.\n"
                        "Do not sound like a consultant unless the interviewer explicitly asks for that.\n"
                    ))
                    SimulatedInterviewMessage.objects.create(interview=interview, role="employee", content=emp_final, turn_index=turn)
                    transcript.append({"role": "user", "content": emp_final})
                    employee_answers.append(emp_final)
                    turn += 1

                    # Extraction
                    transcript_text = build_transcript_text(transcript)
                    extraction_prompt = (
                        "Extract the most important business improvement opportunities from this interview.\n"
                        "Rules:\n"
                        "- Return 2 to 4 opportunities, ordered by importance\n"
                        "- Be concise and specific\n"
                        "- Ground every opportunity in what the employee actually said\n"
                        "- confidence must be between 0.0 and 1.0\n"
                        "- severity must be between 1 and 5\n"
                        "- Do not invent facts not present in the transcript\n\n"
                        f"TRANSCRIPT:\n{transcript_text}"
                    )

                    try:
                        parsed = extract_structured(client, EXTRACTION_SYSTEM, extraction_prompt, EXTRACTION_JSON_SCHEMA)
                        opportunities = validate_opportunities(parsed.get("opportunities"))
                        if opportunities:
                            interview.improvement_opportunities = {"opportunities": opportunities}
                            stats["extractions_valid"] += 1
                            stats["total_opportunities"] += len(opportunities)
                        else:
                            interview.improvement_opportunities = {"opportunities": []}
                            stats["extractions_failed"] += 1
                            self.stderr.write(f"[WARN] {emp_code}: extraction returned no valid opportunities")
                    except Exception as exc:
                        interview.improvement_opportunities = {"opportunities": []}
                        stats["extractions_failed"] += 1
                        self.stderr.write(f"[WARN] {emp_code}: extraction failed ({exc!r})")

                    interview.save(update_fields=["improvement_opportunities"])
                    employee.work_context_summary = "\n---\n".join(employee_answers)[:5000]
                    employee.save(update_fields=["work_context_summary"])
                    stats["succeeded"] += 1

                if pause > 0:
                    time.sleep(pause)

            except Exception as e:
                stats["failed"] += 1
                self.stderr.write(f"[ERROR] Failed interview for {emp_code}: {e!r}")
                continue

        # Summary
        self.stdout.write("\n--- Run Summary ---")
        self.stdout.write(f"  Interviews attempted:     {n}")
        self.stdout.write(f"  Interviews succeeded:     {stats['succeeded']}")
        self.stdout.write(f"  Interviews failed:        {stats['failed']}")
        self.stdout.write(f"  Extractions valid:        {stats['extractions_valid']}")
        self.stdout.write(f"  Extractions failed/empty: {stats['extractions_failed']}")
        self.stdout.write(f"  Total opportunities:      {stats['total_opportunities']}")
        if stats["extractions_valid"] > 0:
            self.stdout.write(f"  Avg opportunities/interview: {stats['total_opportunities'] / stats['extractions_valid']:.1f}")
        self.stdout.write("-------------------\n")
        self.stdout.write(self.style.SUCCESS(f"Simulated {stats['succeeded']} interviews."))
