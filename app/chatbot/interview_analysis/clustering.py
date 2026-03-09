import heapq
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .embedding import cosine_similarity, cosine_distance
from .extraction import IssueRecord


# ---------------------------------------------------------------------------
# kNN graph construction
#
# TODO: _topk_neighbors_for_index is O(n) per call, making the full graph
# O(n²).
# ---------------------------------------------------------------------------
def _topk_neighbors_for_index(
    vectors: List[List[float]],
    i: int,
    k: int,
) -> List[Tuple[int, float]]:
    heap: List[Tuple[float, int]] = []
    vi = vectors[i]

    for j, vj in enumerate(vectors):
        if i == j:
            continue
        sim = cosine_similarity(vi, vj)

        if len(heap) < k:
            heapq.heappush(heap, (sim, j))
        elif sim > heap[0][0]:
            heapq.heapreplace(heap, (sim, j))

    result = [(j, sim) for sim, j in heap]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Reciprocal kNN community clustering
# ---------------------------------------------------------------------------
def reciprocal_knn_community_cluster_cosine(
    vectors: List[List[float]],
    *,
    distance_threshold: float = 0.20,
    min_cluster_size: int = 2,
    knn_k: int = 12,
    min_shared_neighbors: int = 1,
    label_prop_iters: int = 25,
) -> List[int]:
    n = len(vectors)
    if n == 0:
        return []
    if n == 1:
        return [-1 if min_cluster_size > 1 else 0]

    k = max(1, min(knn_k, n - 1))
    sim_threshold = 1.0 - distance_threshold

    neighbor_sets: List[Set[int]] = []
    sim_lookup: List[Dict[int, float]] = []

    for i in range(n):
        nbrs = _topk_neighbors_for_index(vectors, i, k=k)
        cur_set: Set[int] = set()
        cur_map: Dict[int, float] = {}
        for j, sim in nbrs:
            if sim < sim_threshold:
                continue
            cur_set.add(j)
            cur_map[j] = sim
        neighbor_sets.append(cur_set)
        sim_lookup.append(cur_map)

    # Build reciprocal + shared-neighbor adjacency
    adjacency: List[Dict[int, float]] = [dict() for _ in range(n)]
    for i in range(n):
        for j in neighbor_sets[i]:
            if i == j or i not in neighbor_sets[j]:
                continue
            shared = len((neighbor_sets[i] & neighbor_sets[j]) - {i, j})
            if shared < min_shared_neighbors:
                continue
            w = 0.5 * (sim_lookup[i].get(j, 0.0) + sim_lookup[j].get(i, 0.0))
            adjacency[i][j] = w
            adjacency[j][i] = w

    # Weighted label propagation
    labels = list(range(n))
    degrees = [sum(adjacency[i].values()) for i in range(n)]
    node_order = sorted(range(n), key=lambda x: (-degrees[x], x))

    for _ in range(max(1, label_prop_iters)):
        changed = False
        for i in node_order:
            if not adjacency[i]:
                continue

            scores: Dict[int, float] = {}
            for j, w in adjacency[i].items():
                lab = labels[j]
                scores[lab] = scores.get(lab, 0.0) + w

            scores[labels[i]] = scores.get(labels[i], 0.0) + 1e-6

            best_label = labels[i]
            best_score = float("-inf")
            for lab in sorted(scores.keys()):
                sc = scores[lab]
                if sc > best_score or (sc == best_score and lab < best_label):
                    best_score = sc
                    best_label = lab

            if best_label != labels[i]:
                labels[i] = best_label
                changed = True

        if not changed:
            break

    # Filter small clusters to noise
    raw_clusters: Dict[int, List[int]] = {}
    for idx, lab in enumerate(labels):
        raw_clusters.setdefault(lab, []).append(idx)

    final_labels = [-1] * n
    next_label = 0
    for members in raw_clusters.values():
        if len(members) < min_cluster_size:
            continue
        for idx in members:
            final_labels[idx] = next_label
        next_label += 1

    return final_labels


# ---------------------------------------------------------------------------
# Deduplication — embedding-based with fallback
# ---------------------------------------------------------------------------
def deduplicate_issue_records(
    issues: List[IssueRecord],
    vectors: Optional[List[List[float]]] = None,
    similarity_threshold: float = 0.92,
) -> Tuple[List[IssueRecord], List[List[float]]]:
    """Remove near-duplicate issues within the same interview."""
    by_interview: Dict[int, List[int]] = {}
    for idx, r in enumerate(issues):
        by_interview.setdefault(r.interview_id, []).append(idx)

    keep_indices: List[int] = []

    for _, member_indices in by_interview.items():
        ordered = sorted(
            member_indices,
            key=lambda i: (issues[i].confidence * issues[i].severity),
            reverse=True,
        )

        kept_in_interview: List[int] = []
        for idx in ordered:
            is_dup = False
            for prev_idx in kept_in_interview:
                if vectors is not None:
                    sim = cosine_similarity(vectors[idx], vectors[prev_idx])
                    if sim >= similarity_threshold:
                        is_dup = True
                        break
                else:
                    ni = _norm_text(issues[idx].issue)
                    nr = _norm_text(issues[idx].root_cause)
                    pi = _norm_text(issues[prev_idx].issue)
                    pr = _norm_text(issues[prev_idx].root_cause)

                    a, b = set(ni.split()), set(pi.split())
                    j_issue = len(a & b) / max(1, len(a | b))

                    a2, b2 = set(nr.split()), set(pr.split())
                    j_root = len(a2 & b2) / max(1, len(a2 | b2))

                    if max(j_issue, (j_issue + j_root) / 2.0) >= similarity_threshold:
                        is_dup = True
                        break

            if not is_dup:
                kept_in_interview.append(idx)

        keep_indices.extend(kept_in_interview)

    keep_indices.sort()
    filtered_issues = [issues[i] for i in keep_indices]
    filtered_vectors = [vectors[i] for i in keep_indices] if vectors else []
    return filtered_issues, filtered_vectors


def _norm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


# ---------------------------------------------------------------------------
# Cluster utilities
# ---------------------------------------------------------------------------
def compute_cluster_medoid(cluster_indices: List[int], vectors: List[List[float]]) -> int:
    best_idx = cluster_indices[0]
    best_sum = float("inf")
    for i in cluster_indices:
        s = sum(cosine_distance(vectors[i], vectors[j]) for j in cluster_indices)
        if s < best_sum:
            best_sum = s
            best_idx = i
    return best_idx


def nearest_examples_to_medoid(
    cluster_indices: List[int],
    medoid_idx: int,
    vectors: List[List[float]],
    top_n: int = 5,
) -> List[int]:
    ranked = sorted(
        cluster_indices,
        key=lambda idx: cosine_distance(vectors[idx], vectors[medoid_idx]),
    )
    return ranked[:top_n]


def merge_similar_clusters(
    by_cluster: Dict[int, List[int]],
    vectors: List[List[float]],
    similarity_threshold: float = 0.90,
) -> Dict[int, List[int]]:
    cluster_ids = sorted(by_cluster.keys())
    if len(cluster_ids) <= 1:
        return by_cluster

    medoids = {
        cid: compute_cluster_medoid(members, vectors)
        for cid, members in by_cluster.items()
    }

    parent = {cid: cid for cid in cluster_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for i, cid1 in enumerate(cluster_ids):
        for cid2 in cluster_ids[i + 1 :]:
            sim = cosine_similarity(vectors[medoids[cid1]], vectors[medoids[cid2]])
            if sim >= similarity_threshold:
                union(cid1, cid2)

    merged: Dict[int, List[int]] = {}
    for cid, members in by_cluster.items():
        root = find(cid)
        merged.setdefault(root, []).extend(members)

    reindexed: Dict[int, List[int]] = {}
    for new_cid, old_cid in enumerate(sorted(merged.keys())):
        reindexed[new_cid] = sorted(set(merged[old_cid]))

    return reindexed


def cluster_size_stats(by_cluster: Dict[int, List[int]]) -> Dict[str, Any]:
    sizes = sorted((len(v) for v in by_cluster.values()), reverse=True)
    if not sizes:
        return {
            "n_clusters": 0,
            "largest_cluster_size": 0,
            "average_cluster_size": 0.0,
            "top3_cluster_coverage": 0.0,
            "cluster_sizes_desc": [],
        }

    total = sum(sizes)
    return {
        "n_clusters": len(sizes),
        "largest_cluster_size": sizes[0],
        "average_cluster_size": round(total / len(sizes), 3),
        "top3_cluster_coverage": round(sum(sizes[:3]) / total, 4),
        "cluster_sizes_desc": sizes[:20],
    }
