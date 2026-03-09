from typing import Any, Dict, List, Set

from openai import OpenAI

from chatbot.models import SimulatedInterview

from .config import (
    EMBED_MODEL,
    MODEL,
    SUPPORT_MIN_INTERVIEWS,
    WEIGHT_CONFIDENCE,
    WEIGHT_DEPT_SPREAD,
    WEIGHT_FREQUENCY,
    WEIGHT_SEVERITY,
)
from .extraction import IssueRecord, canonicalize_and_persist_extraction
from .embedding import embed_texts, normalize_vectors
from .clustering import (
    cluster_size_stats,
    compute_cluster_medoid,
    deduplicate_issue_records,
    merge_similar_clusters,
    nearest_examples_to_medoid,
    reciprocal_knn_community_cluster_cosine,
)
from .evidence import (
    best_evidence_quote_from_sentence_embeddings,
    build_employee_message_cache,
    build_employee_sentence_cache,
    build_sentence_embedding_cache,
)
from .board import default_board_fallback, synthesize_board_recommendation
from .openai_helpers import chat_completion_json_schema
from .schemas import THEME_LABEL_JSON_SCHEMA


# ---------------------------------------------------------------------------
# Theme labelling
# ---------------------------------------------------------------------------
def summarize_theme_label(
    client: OpenAI,
    medoid_record: IssueRecord,
    example_records: List[IssueRecord],
) -> Dict[str, str]:
    lines = []
    for i, r in enumerate(example_records[:5], start=1):
        lines.append(
            f"{i}. Issue: {r.issue}\n"
            f"   Impact: {r.impact}\n"
            f"   Root cause: {r.root_cause}\n"
            f"   Suggested action: {r.suggested_action}"
        )

    system_prompt = (
        "You summarise clustered interview issues into concise business themes. "
        "Return only valid JSON that matches the schema."
    )

    user_prompt = (
        "Create a concise board-friendly theme title and one-sentence summary.\n"
        "Requirements:\n"
        "- Theme title: max 8 words\n"
        "- Use abstract business language, not conversational phrasing\n"
        "- Avoid repeating full issue sentences verbatim\n"
        "- Preserve the underlying meaning of the examples\n\n"
        f"MEDOID ISSUE:\n"
        f"Issue: {medoid_record.issue}\n"
        f"Impact: {medoid_record.impact}\n"
        f"Root cause: {medoid_record.root_cause}\n"
        f"Suggested action: {medoid_record.suggested_action}\n\n"
        f"NEAREST EXAMPLES:\n" + "\n".join(lines)
    )

    parsed = chat_completion_json_schema(
        client,
        model=MODEL,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=THEME_LABEL_JSON_SCHEMA,
        temperature=0.0,
    )

    return {
        "theme_label": str(parsed.get("theme_label") or "").strip() or medoid_record.issue[:80],
        "theme_summary": str(parsed.get("theme_summary") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run_analysis(
    client: OpenAI,
    interviews: List[SimulatedInterview],
    *,
    regen_missing: bool = False,
    min_confidence: float = 0.2,
    distance_threshold: float = 0.20,
    min_cluster_size: int = 2,
    dedup_within_interview: bool = True,
    cluster_knn_k: int = 12,
    cluster_min_shared_neighbors: int = 1,
    cluster_label_prop_iters: int = 25,
    cluster_merge_threshold: float = 0.90,
    log=None,
) -> Dict[str, Any]:
    """Run the full analysis pipeline and return the report dict."""

    def _log(msg: str) -> None:
        if log:
            log(msg)

    if not interviews:
        return {"error": "No interviews found"}

    all_departments = sorted({
        iv.employee.department
        for iv in interviews
        if getattr(iv, "employee", None) and iv.employee and iv.employee.department
    })
    total_departments = max(1, len(all_departments))
    N = len(interviews)

    interview_ids = [iv.id for iv in interviews]
    message_cache = build_employee_message_cache(interview_ids)
    sentence_cache = build_employee_sentence_cache(message_cache)

    # ------------------------------------------------------------------
    # 1. Extract issues
    # ------------------------------------------------------------------
    issues: List[IssueRecord] = []
    skipped_no_opps = 0
    severity_llm_count = 0
    severity_regex_count = 0

    for iv in interviews:
        employee = iv.employee
        ex = canonicalize_and_persist_extraction(client, iv, regen_missing=regen_missing)

        items = ex.get("opportunities") or []
        if not isinstance(items, list):
            items = []

        if not items:
            skipped_no_opps += 1

        for it in items:
            if not isinstance(it, dict):
                continue

            issue = (it.get("issue") or "").strip()
            if not issue:
                continue

            conf = it.get("confidence", 0.5)
            if not isinstance(conf, (int, float)) or conf < min_confidence:
                continue

            sev_source = it.get("severity_source", "llm")
            if sev_source == "regex_fallback":
                severity_regex_count += 1
            else:
                severity_llm_count += 1

            issues.append(
                IssueRecord(
                    interview_id=iv.id,
                    employee_code=employee.employee_code,
                    department=employee.department or "",
                    role_title=employee.role_title or "",
                    issue=issue,
                    impact=(it.get("impact") or "").strip(),
                    root_cause=(it.get("root_cause") or "").strip(),
                    suggested_action=(it.get("suggested_action") or "").strip(),
                    confidence=conf,
                    severity=it.get("severity", 2.5),
                    severity_source=sev_source,
                    evidence_quote="",
                )
            )

    _log(
        f"Extracted {len(issues)} issues from {N} interviews "
        f"({skipped_no_opps} with no opportunities, "
        f"severity: {severity_llm_count} LLM / {severity_regex_count} regex fallback)"
    )

    if not issues:
        return {"error": "No issues extracted (check improvement_opportunities or enable --regen-missing)."}

    # ------------------------------------------------------------------
    # 2. Embed
    # ------------------------------------------------------------------
    issue_texts = [
        f"Issue: {x.issue}\nBusiness impact: {x.impact}\n"
        f"Likely root cause: {x.root_cause}\nSuggested action: {x.suggested_action}"
        for x in issues
    ]

    vectors = normalize_vectors(embed_texts(client, issue_texts))
    if len(vectors) != len(issues):
        raise RuntimeError("Embedding count mismatch vs issues count.")

    # ------------------------------------------------------------------
    # 3. Deduplicate
    # ------------------------------------------------------------------
    if dedup_within_interview:
        issues, vectors = deduplicate_issue_records(issues, vectors=vectors)
        _log(f"After dedup: {len(issues)} issues remain")

    if not issues:
        return {"error": "All issues removed after deduplication."}

    # ------------------------------------------------------------------
    # 4. Cluster
    # ------------------------------------------------------------------
    labels = reciprocal_knn_community_cluster_cosine(
        vectors=vectors,
        distance_threshold=distance_threshold,
        min_cluster_size=min_cluster_size,
        knn_k=cluster_knn_k,
        min_shared_neighbors=cluster_min_shared_neighbors,
        label_prop_iters=cluster_label_prop_iters,
    )

    by_cluster: Dict[int, List[int]] = {}
    for idx, lab in enumerate(labels):
        if lab != -1:
            by_cluster.setdefault(lab, []).append(idx)

    if not by_cluster:
        return {
            "error": (
                "No clusters found above minimum size. "
                "Try increasing --distance-threshold, lowering --min-cluster-size, "
                "or increasing --cluster-knn-k."
            )
        }

    by_cluster = merge_similar_clusters(
        by_cluster=by_cluster,
        vectors=vectors,
        similarity_threshold=cluster_merge_threshold,
    )

    if not by_cluster:
        return {"error": "No clusters remain after post-merge step."}

    _log(f"Formed {len(by_cluster)} clusters from {len(issues)} issues")

    # ------------------------------------------------------------------
    # 5. Evidence quotes
    # ------------------------------------------------------------------
    clustered_interview_ids: Set[int] = set()
    for member_indices in by_cluster.values():
        for idx in member_indices:
            clustered_interview_ids.add(issues[idx].interview_id)

    sentence_embedding_cache = build_sentence_embedding_cache(
        client, sentence_cache, clustered_interview_ids=clustered_interview_ids,
    )

    for idx in range(len(issues)):
        if issues[idx].interview_id not in clustered_interview_ids:
            continue
        sentence_vectors = sentence_embedding_cache.get(issues[idx].interview_id, [])
        issues[idx].evidence_quote = best_evidence_quote_from_sentence_embeddings(
            sentence_vectors=sentence_vectors,
            query_vector=vectors[idx],
            issue_text=issues[idx].issue,
            root_cause_text=issues[idx].root_cause,
        )

    # ------------------------------------------------------------------
    # 6. Score and rank themes
    # ------------------------------------------------------------------
    themes: List[Dict[str, Any]] = []

    for lab, member_indices in by_cluster.items():
        recs = [issues[i] for i in member_indices]

        unique_interviews = len(set(r.interview_id for r in recs))
        freq = unique_interviews / float(N)

        avg_sev = sum(r.severity for r in recs) / len(recs)
        avg_conf = sum(r.confidence for r in recs) / len(recs)

        unique_departments = sorted({r.department for r in recs if r.department})
        department_spread = len(unique_departments)
        department_spread_ratio = department_spread / float(total_departments)

        base_score = (
            WEIGHT_SEVERITY * (avg_sev / 5.0)
            + WEIGHT_FREQUENCY * freq
            + WEIGHT_CONFIDENCE * avg_conf
            + WEIGHT_DEPT_SPREAD * department_spread_ratio
        )
        support_multiplier = min(1.0, unique_interviews / float(SUPPORT_MIN_INTERVIEWS))
        score = base_score * support_multiplier

        medoid_idx = compute_cluster_medoid(member_indices, vectors)
        nearest_idxs = nearest_examples_to_medoid(member_indices, medoid_idx, vectors, top_n=5)

        medoid_rec = issues[medoid_idx]
        nearest_recs = [issues[i] for i in nearest_idxs]

        try:
            label_payload = summarize_theme_label(client, medoid_rec, nearest_recs)
            theme_label = label_payload["theme_label"] or medoid_rec.issue.strip() or f"Theme {lab}"
            theme_summary = label_payload["theme_summary"]
        except Exception:
            theme_label = medoid_rec.issue.strip() or f"Theme {lab}"
            theme_summary = ""

        top_examples = [
            {
                "interview_id": issues[i].interview_id,
                "employee_code": issues[i].employee_code,
                "department": issues[i].department,
                "role_title": issues[i].role_title,
                "issue": issues[i].issue,
                "impact": issues[i].impact,
                "root_cause": issues[i].root_cause,
                "suggested_action": issues[i].suggested_action,
                "confidence": issues[i].confidence,
                "severity": issues[i].severity,
                "evidence_quote": issues[i].evidence_quote,
            }
            for i in nearest_idxs
        ]

        themes.append({
            "cluster_id": lab,
            "theme_label": theme_label,
            "theme_summary": theme_summary,
            "medoid_issue": medoid_rec.issue,
            "medoid_root_cause": medoid_rec.root_cause,
            "score": round(score, 4),
            "base_score": round(base_score, 4),
            "support_multiplier": round(support_multiplier, 4),
            "frequency_interviews": unique_interviews,
            "frequency_ratio": round(freq, 4),
            "avg_severity": round(avg_sev, 3),
            "avg_confidence": round(avg_conf, 3),
            "n_issue_records": len(recs),
            "department_spread": department_spread,
            "department_spread_ratio": round(department_spread_ratio, 4),
            "departments": unique_departments,
            "examples": top_examples,
        })

    themes.sort(
        key=lambda t: (t["score"], t["frequency_interviews"], t["department_spread"]),
        reverse=True,
    )
    top_theme = themes[0]
    noise_count = sum(1 for lab in labels if lab == -1)

    # ------------------------------------------------------------------
    # 7. Board recommendation
    # ------------------------------------------------------------------
    try:
        board_payload = synthesize_board_recommendation(
            client=client, top_theme=top_theme, n_interviews=N,
        )
    except Exception:
        board_payload = default_board_fallback(top_theme, N)

    # ------------------------------------------------------------------
    # 8. Assemble report
    # ------------------------------------------------------------------
    score_formula = (
        f"base_score = {WEIGHT_SEVERITY}*(avg_severity/5) + {WEIGHT_FREQUENCY}*frequency_ratio + "
        f"{WEIGHT_CONFIDENCE}*avg_confidence + {WEIGHT_DEPT_SPREAD}*department_spread_ratio; "
        f"score = base_score * min(1, frequency_interviews/{SUPPORT_MIN_INTERVIEWS})"
    )

    total_severity_scored = severity_llm_count + severity_regex_count
    severity_source_summary = "100% LLM"
    if severity_regex_count > 0 and severity_llm_count > 0:
        pct = round(100.0 * severity_llm_count / total_severity_scored, 1)
        severity_source_summary = f"{pct}% LLM, {round(100.0 - pct, 1)}% regex fallback"
    elif severity_regex_count > 0:
        severity_source_summary = "100% regex fallback"

    return {
        "metadata": {
            "n_interviews_analyzed": N,
            "n_issue_records": len(issues),
            "n_noise_points": noise_count,
            "n_interviews_with_no_opportunities": skipped_no_opps,
            "embedding_model": EMBED_MODEL,
            "llm_model": MODEL,
            "score_formula": score_formula,
            "min_confidence": min_confidence,
            "regen_missing": regen_missing,
            "clustering_method": "reciprocal_knn_shared_neighbor_label_propagation_pure_python",
            "distance_threshold": distance_threshold,
            "min_cluster_size": min_cluster_size,
            "cluster_knn_k": cluster_knn_k,
            "cluster_min_shared_neighbors": cluster_min_shared_neighbors,
            "cluster_label_prop_iters": cluster_label_prop_iters,
            "cluster_merge_threshold": cluster_merge_threshold,
            "dedup_within_interview": dedup_within_interview,
            "dedup_method": "cosine_embedding" if dedup_within_interview else "none",
            "severity_source": severity_source_summary,
            "severity_llm_count": severity_llm_count,
            "severity_regex_fallback_count": severity_regex_count,
            "theme_labeling_method": "llm_label_from_medoid_and_nearest_examples",
            "extraction_method": "structured_json_schema",
            "board_recommendation_method": "structured_json_schema",
            "evidence_extraction_level": "sentence",
            "evidence_selection_method": "hybrid_semantic_lexical_sentence_retrieval",
            "evidence_scope": "clustered_interviews_only",
            "total_departments_in_sample": total_departments,
            "departments_in_sample": all_departments,
            **cluster_size_stats(by_cluster),
        },
        "top_theme": top_theme,
        "themes_ranked": themes[: min(15, len(themes))],
        "board_recommendation": board_payload,
    }
