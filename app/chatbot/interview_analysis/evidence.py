import re
from typing import Dict, List, Set, Tuple

from openai import OpenAI

from chatbot.models import SimulatedInterviewMessage

from .embedding import embed_texts, normalize_vectors, cosine_similarity


# ---------------------------------------------------------------------------
# Transcript / sentence helpers
# ---------------------------------------------------------------------------
def build_employee_message_cache(interview_ids: List[int]) -> Dict[int, List[str]]:
    cache: Dict[int, List[str]] = {iid: [] for iid in interview_ids}
    rows = (
        SimulatedInterviewMessage.objects
        .filter(interview_id__in=interview_ids, role="employee")
        .only("interview_id", "content", "turn_index")
        .order_by("interview_id", "turn_index")
    )
    for row in rows:
        cache.setdefault(row.interview_id, []).append((row.content or "").strip())
    return cache


def split_text_into_sentences(text: str) -> List[str]:
    if not text:
        return []
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|(?<=[;:])\s+", cleaned)
    out: List[str] = []
    for p in parts:
        s = p.strip(" -\t\r\n")
        if s:
            out.append(s)
    return out or [cleaned]


def build_employee_sentence_cache(message_cache: Dict[int, List[str]]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    for interview_id, messages in message_cache.items():
        seen: Set[str] = set()
        ordered: List[str] = []
        for msg in messages:
            for sent in split_text_into_sentences(msg):
                s = sent.strip()
                if s and s not in seen:
                    seen.add(s)
                    ordered.append(s)
        out[interview_id] = ordered
    return out


def build_sentence_embedding_cache(
    client: OpenAI,
    sentence_cache: Dict[int, List[str]],
    *,
    clustered_interview_ids: Set[int],
    batch_size: int = 128,
) -> Dict[int, List[Tuple[str, List[float]]]]:
    """Embed sentences only for interviews that produced clustered issues."""
    flattened: List[str] = []
    owners: List[int] = []

    for interview_id, sentences in sentence_cache.items():
        if interview_id not in clustered_interview_ids:
            continue
        for s in sentences:
            flattened.append(s)
            owners.append(interview_id)

    out: Dict[int, List[Tuple[str, List[float]]]] = {
        iid: [] for iid in clustered_interview_ids
    }
    if not flattened:
        return out

    vectors = normalize_vectors(embed_texts(client, flattened, batch_size=batch_size))
    for interview_id, sentence, vec in zip(owners, flattened, vectors):
        out.setdefault(interview_id, []).append((sentence, vec))

    return out


# ---------------------------------------------------------------------------
# Keyword / scoring helpers
# ---------------------------------------------------------------------------
def keyword_set(text: str) -> Set[str]:
    text = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    words = [w for w in text.split() if len(w) >= 4]
    stop = {
        "that", "this", "with", "from", "have", "were", "their", "about", "because",
        "would", "could", "should", "there", "which", "while", "into", "they",
        "them", "than", "then", "also", "been", "being", "over", "under", "after",
        "before", "where", "when", "what", "your", "ours", "ourselves", "itself",
    }
    return {w for w in words if w not in stop}


def contradiction_penalty(sentence: str, issue_text: str) -> float:
    s = (sentence or "").lower()
    issue_tokens = keyword_set(issue_text)

    penalty = 0.0
    if any(x in s for x in ["more than", "rather than", "instead of", "not the main", "bigger problem", "more often"]):
        penalty += 0.10
    if "while" in s and any(tok in s for tok in issue_tokens):
        penalty += 0.05
    if re.search(r"\bbut\b", s) and any(tok in s for tok in issue_tokens):
        penalty += 0.03
    return penalty


def best_evidence_quote_from_sentence_embeddings(
    sentence_vectors: List[Tuple[str, List[float]]],
    query_vector: List[float],
    issue_text: str,
    root_cause_text: str,
    max_len: int = 220,
) -> str:
    if not sentence_vectors or not query_vector:
        return ""

    issue_tokens = keyword_set(issue_text)
    root_tokens = keyword_set(root_cause_text)

    best_sent = ""
    best_score = float("-inf")

    for sent, sent_vec in sentence_vectors:
        sim = cosine_similarity(query_vector, sent_vec)

        sent_tokens = keyword_set(sent)
        lexical_issue = len(sent_tokens & issue_tokens) / max(1, len(issue_tokens))
        lexical_root = len(sent_tokens & root_tokens) / max(1, len(root_tokens))

        score = (
            0.75 * sim
            + 0.20 * lexical_issue
            + 0.05 * lexical_root
            - contradiction_penalty(sent, issue_text)
        )

        if len(sent) < 25:
            score -= 0.03
        elif 40 <= len(sent) <= 180:
            score += 0.01
        elif len(sent) > 280:
            score -= 0.02

        if score > best_score:
            best_score = score
            best_sent = sent

    if not best_sent:
        return ""

    s = best_sent.replace("\n", " ").strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s
