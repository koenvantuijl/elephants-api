#!/bin/sh
set -eu

# -----------------------------------------------------------------------------
# Determine DB mode / paths
# -----------------------------------------------------------------------------
use_sqlite="${USE_SQLITE:-0}"
sqlite_path="${SQLITE_PATH:-/home/site/db.sqlite3}"

has_db_config=false
if [ -n "${DATABASE_URL:-}" ] || [ -n "${POSTGRES_HOST:-}" ]; then
  has_db_config=true
fi

# -----------------------------------------------------------------------------
# 0) Ensure persistent storage for SQLite on Azure App Service
# -----------------------------------------------------------------------------
if [ "$use_sqlite" = "1" ]; then
  mkdir -p "$(dirname "$sqlite_path")"
  echo "SQLite mode enabled. SQLITE_PATH=$sqlite_path"

  if ! touch "$sqlite_path" 2>/dev/null; then
    echo "ERROR: SQLite path is not writable: $sqlite_path"
    echo "Check WEBSITES_ENABLE_APP_SERVICE_STORAGE=true and permissions on /home."
    ls -ld "$(dirname "$sqlite_path")" || true
    exit 1
  fi
fi

# -----------------------------------------------------------------------------
# Optional DB reset (SQLite only)
# -----------------------------------------------------------------------------
if [ "$use_sqlite" = "1" ] && [ "${RESET_DB:-0}" = "1" ]; then
  echo "RESET_DB=1 -> removing SQLite db at ${sqlite_path}"
  rm -f "$sqlite_path"

  echo "RESET_DB=1 -> removing marker files"
  rm -f /home/site/.seed_done_* 2>/dev/null || true
  rm -f /home/site/.seed_interviews_done_* 2>/dev/null || true
  rm -f /home/site/.analyze_done_* 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# 1) Optional wait for Postgres
# -----------------------------------------------------------------------------
if [ -n "${POSTGRES_HOST:-}" ]; then
  echo "Waiting for Postgres at ${POSTGRES_HOST}:${POSTGRES_PORT:-5432}..."
  python - <<'PY'
import os
import time
import psycopg2

host = os.getenv("POSTGRES_HOST")
port = int(os.getenv("POSTGRES_PORT", "5432"))
name = os.getenv("POSTGRES_DB")
user = os.getenv("POSTGRES_USER")
password = os.getenv("POSTGRES_PASSWORD")

missing = [k for k, v in {
    "POSTGRES_DB": name,
    "POSTGRES_USER": user,
    "POSTGRES_PASSWORD": password,
    "POSTGRES_HOST": host,
}.items() if not v]

if missing:
    raise SystemExit(f"Missing required Postgres env vars: {', '.join(missing)}")

deadline = time.time() + 60
while True:
    try:
        psycopg2.connect(
            host=host,
            port=port,
            dbname=name,
            user=user,
            password=password,
        ).close()
        print("Postgres is reachable.")
        break
    except Exception as e:
        if time.time() > deadline:
            raise SystemExit(f"Timed out waiting for Postgres: {e}")
        time.sleep(1)
PY
fi

# -----------------------------------------------------------------------------
# 2) Migrations
# -----------------------------------------------------------------------------
if [ "$use_sqlite" = "1" ]; then
  echo "USE_SQLITE=1 -> running migrations on SQLite..."
  export SQLITE_PATH="$sqlite_path"
  python manage.py migrate --noinput || exit 1
elif [ "$has_db_config" = "true" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput || exit 1
else
  echo "No DATABASE_URL / POSTGRES_HOST set; attempting migrations with default DB..."
  python manage.py migrate --noinput || exit 1
fi

# -----------------------------------------------------------------------------
# 2b) Background seeding (conversations) with marker
# -----------------------------------------------------------------------------
seed_conversations="${SEED_CONVERSATIONS:-1}"
seed_conversations_total="${SEED_N:-100}"
seed_conversations_batch="${SEED_BATCH:-5}"
seed_conversations_sleep="${SEED_SLEEP:-2}"
seed_conversations_marker="${SEED_MARKER_PATH:-/home/site/.seed_done_${seed_conversations_total}}"

if [ "$seed_conversations" = "1" ]; then
  [ "$use_sqlite" = "1" ] && export SQLITE_PATH="$sqlite_path"
  export SEED_N="$seed_conversations_total"
  export SEED_BATCH="$seed_conversations_batch"
  export SEED_SLEEP="$seed_conversations_sleep"
  export SEED_MARKER_PATH="$seed_conversations_marker"

  if [ -f "$seed_conversations_marker" ]; then
    echo "Conversation seeding skipped: marker exists ($seed_conversations_marker)."
  else
    echo "Conversation seeding enabled (total=${seed_conversations_total}, batch=${seed_conversations_batch}, sleep=${seed_conversations_sleep}s)."
    (
      python - <<'PY'
import os
import time
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE", "config.settings"))
django.setup()

from django.core.management import call_command
from chatbot.models import SimulatedConversation

total = int(os.getenv("SEED_N", "100"))
batch = int(os.getenv("SEED_BATCH", "5"))
sleep_s = float(os.getenv("SEED_SLEEP", "2"))
marker = os.getenv("SEED_MARKER_PATH")

if marker and os.path.exists(marker):
    print(f"[seed_conversations] marker exists -> skip ({marker})")
    raise SystemExit(0)

while True:
    current = SimulatedConversation.objects.count()
    if current >= total:
        print(f"[seed_conversations] done (current={current} >= total={total})")
        break

    remaining = total - current
    n = batch if remaining > batch else remaining
    print(
        f"[seed_conversations] simulate_conversations --n {n} "
        f"(current={current}, remaining={remaining})"
    )
    call_command("simulate_conversations", n=n)

    current_after = SimulatedConversation.objects.count()
    print(f"[seed_conversations] progress: current={current_after}/{total}")

    if current_after >= total:
        break

    time.sleep(sleep_s)

if marker:
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"seeded_total={total}\n")
        print(f"[seed_conversations] marker written: {marker}")
    except Exception as e:
        print(f"[seed_conversations] WARN: could not write marker {marker}: {e!r}")
PY
    ) &
  fi
else
  echo "Conversation seeding disabled (SEED_CONVERSATIONS=$seed_conversations)."
fi

# -----------------------------------------------------------------------------
# 2c0) Optional force rerun of analyze_interviews without clearing interviews
# -----------------------------------------------------------------------------
force_rerun_analyze="${FORCE_RERUN_ANALYZE:-0}"

if [ "$force_rerun_analyze" = "1" ]; then
  echo "FORCE_RERUN_ANALYZE=1 -> removing analyze markers only"
  rm -f /home/site/.analyze_done_* 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# 2c + 2d) Background interview seeding followed by board insight analysis
# -----------------------------------------------------------------------------
seed_interviews="${SEED_INTERVIEWS:-0}"
seed_interviews_total="${SEED_INTERVIEWS_N:-100}"
seed_interviews_batch="${SEED_INTERVIEWS_BATCH:-5}"
seed_interviews_sleep="${SEED_INTERVIEWS_SLEEP:-2}"
seed_interviews_marker="${SEED_INTERVIEWS_MARKER_PATH:-/home/site/.seed_interviews_done_${seed_interviews_total}}"

analyze_interviews="${ANALYZE_INTERVIEWS:-1}"
analyze_min_interviews="${ANALYZE_MIN_INTERVIEWS:-100}"
analyze_cluster_knn_k="${ANALYZE_CLUSTER_KNN_K:-12}"
analyze_distance_threshold="${ANALYZE_DISTANCE_THRESHOLD:-0.20}"
analyze_cluster_merge_threshold="${ANALYZE_CLUSTER_MERGE_THRESHOLD:-0.90}"
analyze_marker="${ANALYZE_MARKER_PATH:-/home/site/.analyze_done_${analyze_min_interviews}_${analyze_cluster_knn_k}}"

if [ "$seed_interviews" = "1" ] || [ "$analyze_interviews" = "1" ]; then
  [ "$use_sqlite" = "1" ] && export SQLITE_PATH="$sqlite_path"

  export SEED_INTERVIEWS="$seed_interviews"
  export SEED_INTERVIEWS_N="$seed_interviews_total"
  export SEED_INTERVIEWS_BATCH="$seed_interviews_batch"
  export SEED_INTERVIEWS_SLEEP="$seed_interviews_sleep"
  export SEED_INTERVIEWS_MARKER_PATH="$seed_interviews_marker"

  export ANALYZE_INTERVIEWS="$analyze_interviews"
  export ANALYZE_MIN_INTERVIEWS="$analyze_min_interviews"
  export ANALYZE_CLUSTER_KNN_K="$analyze_cluster_knn_k"
  export ANALYZE_DISTANCE_THRESHOLD="$analyze_distance_threshold"
  export ANALYZE_CLUSTER_MERGE_THRESHOLD="$analyze_cluster_merge_threshold"
  export ANALYZE_MARKER_PATH="$analyze_marker"

  (
    python - <<'PY'
import os
import time
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE", "config.settings"))
django.setup()

from django.core.management import call_command
from chatbot.models import SimulatedInterview

seed_enabled = os.getenv("SEED_INTERVIEWS", "0") == "1"
analyze_enabled = os.getenv("ANALYZE_INTERVIEWS", "1") == "1"

total = int(os.getenv("SEED_INTERVIEWS_N", "100"))
batch = int(os.getenv("SEED_INTERVIEWS_BATCH", "5"))
sleep_s = float(os.getenv("SEED_INTERVIEWS_SLEEP", "2"))
seed_marker = os.getenv("SEED_INTERVIEWS_MARKER_PATH")

analyze_min = int(os.getenv("ANALYZE_MIN_INTERVIEWS", "100"))
cluster_knn_k = int(os.getenv("ANALYZE_CLUSTER_KNN_K", "12"))
distance_threshold = float(os.getenv("ANALYZE_DISTANCE_THRESHOLD", "0.20"))
cluster_merge_threshold = float(os.getenv("ANALYZE_CLUSTER_MERGE_THRESHOLD", "0.90"))
analyze_marker = os.getenv("ANALYZE_MARKER_PATH")

# ------------------------------------------------------------------
# Phase 1: seed interviews
# ------------------------------------------------------------------
if seed_enabled:
    if seed_marker and os.path.exists(seed_marker):
        print(f"[seed_interviews] marker exists -> skip ({seed_marker})")
    else:
        print(
            f"[seed_interviews] enabled "
            f"(total={total}, batch={batch}, sleep={sleep_s}s)"
        )

        while True:
            current = SimulatedInterview.objects.count()
            if current >= total:
                print(f"[seed_interviews] done (current={current} >= total={total})")
                break

            remaining = total - current
            n = batch if remaining > batch else remaining
            print(
                f"[seed_interviews] simulate_interviews --n {n} "
                f"(current={current}, remaining={remaining})"
            )
            call_command("simulate_interviews", n=n)

            current_after = SimulatedInterview.objects.count()
            print(f"[seed_interviews] progress: current={current_after}/{total}")

            if current_after >= total:
                break

            time.sleep(sleep_s)

        if seed_marker:
            try:
                with open(seed_marker, "w", encoding="utf-8") as f:
                    f.write(f"seeded_interviews_total={total}\n")
                print(f"[seed_interviews] marker written: {seed_marker}")
            except Exception as e:
                print(f"[seed_interviews] WARN: could not write marker {seed_marker}: {e!r}")
else:
    print("[seed_interviews] disabled")

# ------------------------------------------------------------------
# Phase 2: analyze interviews
# ------------------------------------------------------------------
if analyze_enabled:
    if analyze_marker and os.path.exists(analyze_marker):
        print(f"[analyze] marker exists -> skip ({analyze_marker})")
    else:
        n = SimulatedInterview.objects.count()

        if n < analyze_min:
            print(
                f"[analyze] skipped: only {n} interviews present, "
                f"minimum required is {analyze_min}"
            )
        else:
            print(
                f"[analyze] running analyze_interviews --n {n} "
                f"--cluster-knn-k {cluster_knn_k} "
                f"--distance-threshold {distance_threshold} "
                f"--cluster-merge-threshold {cluster_merge_threshold}"
            )
            call_command(
                "analyze_interviews",
                n=n,
                cluster_knn_k=cluster_knn_k,
                distance_threshold=distance_threshold,
                cluster_merge_threshold=cluster_merge_threshold,
                min_cluster_size=5,
            )

            if analyze_marker:
                try:
                    with open(analyze_marker, "w", encoding="utf-8") as f:
                        f.write(
                            f"analyzed_n={n}, "
                            f"cluster_knn_k={cluster_knn_k}, "
                            f"distance_threshold={distance_threshold}, "
                            f"cluster_merge_threshold={cluster_merge_threshold}\n"
                        )
                    print(f"[analyze] marker written: {analyze_marker}")
                except Exception as e:
                    print(f"[analyze] WARN: could not write marker {analyze_marker}: {e!r}")
else:
    print("[analyze] disabled")
PY
  ) &
else
  echo "Interview seeding and analyze are both disabled."
fi

# -----------------------------------------------------------------------------
# 3) Static files + start
# -----------------------------------------------------------------------------
echo "Collecting static files..."
python manage.py collectstatic --noinput || true

echo "Starting application..."
exec "$@"