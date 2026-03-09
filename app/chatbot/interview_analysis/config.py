import os

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# ---------------------------------------------------------------------------
# Clustering defaults
# ---------------------------------------------------------------------------
DEFAULT_DISTANCE_THRESHOLD = float(os.environ.get("ANALYZE_DISTANCE_THRESHOLD", "0.20"))
DEFAULT_CLUSTER_MERGE_THRESHOLD = float(os.environ.get("ANALYZE_CLUSTER_MERGE_THRESHOLD", "0.90"))

# ---------------------------------------------------------------------------
# Scoring weights — tune these or load from env
# ---------------------------------------------------------------------------
WEIGHT_SEVERITY = float(os.environ.get("SCORE_WEIGHT_SEVERITY", "0.35"))
WEIGHT_FREQUENCY = float(os.environ.get("SCORE_WEIGHT_FREQUENCY", "0.30"))
WEIGHT_CONFIDENCE = float(os.environ.get("SCORE_WEIGHT_CONFIDENCE", "0.15"))
WEIGHT_DEPT_SPREAD = float(os.environ.get("SCORE_WEIGHT_DEPT_SPREAD", "0.20"))
SUPPORT_MIN_INTERVIEWS = int(os.environ.get("SCORE_SUPPORT_MIN_INTERVIEWS", "5"))
