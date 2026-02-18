#!/bin/sh
set -eu

# -----------------------------------------------------------------------------
# Determine DB mode
# -----------------------------------------------------------------------------
use_sqlite="${USE_SQLITE:-0}"

# Helper: true als we aannemen dat er een externe DB geconfigureerd is
has_db_config=false
if [ -n "${DATABASE_URL:-}" ] || [ -n "${POSTGRES_HOST:-}" ]; then
  has_db_config=true
fi

# -----------------------------------------------------------------------------
# 1) Optional wait for Postgres (only if POSTGRES_HOST is set)
# -----------------------------------------------------------------------------
if [ -n "${POSTGRES_HOST:-}" ]; then
  echo "Waiting for Postgres at ${POSTGRES_HOST}:${POSTGRES_PORT:-5432}..."
  python - <<'PY'
import os, time
import psycopg2

host = os.getenv("POSTGRES_HOST")
port = int(os.getenv("POSTGRES_PORT", "5432"))
name = os.getenv("POSTGRES_DB")
user = os.getenv("POSTGRES_USER")
password = os.getenv("POSTGRES_PASSWORD")

missing = [k for k,v in {
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
        psycopg2.connect(host=host, port=port, dbname=name, user=user, password=password).close()
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
#    - ALWAYS run migrations when USE_SQLITE=1 (SQLite needs schema too)
#    - Otherwise: run migrations if external DB config is present
#    - Else: attempt migrations with default DB
# -----------------------------------------------------------------------------
if [ "$use_sqlite" = "1" ]; then
  echo "USE_SQLITE=1 -> running migrations on SQLite..."
  python manage.py migrate --noinput
elif [ "$has_db_config" = "true" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput
else
  echo "No DATABASE_URL / POSTGRES_HOST set; attempting migrations with default DB..."
  python manage.py migrate --noinput
fi

# -----------------------------------------------------------------------------
# 2b) Background seeding (Strategy 1)
#   - Does NOT block startup (runs in background) to avoid App Service timeouts
#   - Seeds up to SEED_N conversations in batches (SEED_BATCH) with sleeps (SEED_SLEEP)
#   - Only when USE_SQLITE=1 and SEED_CONVERSATIONS=1
# -----------------------------------------------------------------------------
seed="${SEED_CONVERSATIONS:-1}"
seed_total="${SEED_N:-100}"
seed_batch="${SEED_BATCH:-5}"
seed_sleep="${SEED_SLEEP:-2}"

if [ "$use_sqlite" = "1" ] && [ "$seed" = "1" ]; then
  echo "Background seeding enabled (total=${seed_total}, batch=${seed_batch}, sleep=${seed_sleep}s)."
  (
    python - <<'PY'
import os, time
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE", "config.settings"))
django.setup()

from django.core.management import call_command
from chatbot.models import SimulatedConversation

total = int(os.getenv("SEED_N", "100"))
batch = int(os.getenv("SEED_BATCH", "5"))
sleep_s = float(os.getenv("SEED_SLEEP", "2"))

# Seed until we reach "total" conversations
while True:
    current = SimulatedConversation.objects.count()
    if current >= total:
        print(f"[seed] done (current={current} >= total={total}).")
        break

    remaining = total - current
    n = batch if remaining > batch else remaining
    print(f"[seed] simulate_conversations --n {n} (current={current}, remaining={remaining})")
    call_command("simulate_conversations", n=n)

    # Small pause to reduce rate-limit pressure and keep CPU low
    time.sleep(sleep_s)
PY
  ) &
else
  echo "Seeding disabled or not in USE_SQLITE=1 mode (USE_SQLITE=$use_sqlite, SEED_CONVERSATIONS=$seed)."
fi

# -----------------------------------------------------------------------------
# 3) Static files (safe)
# -----------------------------------------------------------------------------
echo "Collecting static files..."
python manage.py collectstatic --noinput || true

echo "Starting application..."
exec "$@"
