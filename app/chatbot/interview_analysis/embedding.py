import math
from typing import List

from openai import OpenAI

from .config import EMBED_MODEL


def embed_texts(client: OpenAI, texts: List[str], batch_size: int = 64) -> List[List[float]]:
    if not texts:
        return []
    vectors: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend([d.embedding for d in resp.data])
    return vectors


def l2_normalize(v: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm <= 0.0:
        return v[:]
    return [x / norm for x in v]


def normalize_vectors(vectors: List[List[float]]) -> List[List[float]]:
    return [l2_normalize(v) for v in vectors]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosine_distance(a: List[float], b: List[float]) -> float:
    return 1.0 - cosine_similarity(a, b)
