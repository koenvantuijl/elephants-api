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
# 0) Ensure persistent storage for SQLite on Azure App Service
#    - /home is persistent when WEBSITES_ENABLE_APP_SERVICE_STORAGE=true
# -----------------------------------------------------------------------------
sqlite_path="${SQLITE_PATH:-/home/site/db.sqlite3}"

if [ "$use_sqlite" = "1" ]; then
  mkdir -p "$(dirname "$sqlite_path")"
  echo "SQLite mode enabled. SQLITE_PATH=$sqlite_path"
fi

# -----------------------------------------------------------------------------
# Optional DB reset (SQLite only)
#   - If RESET_DB=1: delete SQLite file AND seeding marker(s) before migrations
# -----------------------------------------------------------------------------
sqlite_path="${SQLITE_PATH:-/home/site/db.sqlite3}"
seed_marker_glob="/home/site/.seed_done_*"

if [ "$use_sqlite" = "1" ] && [ "${RESET_DB:-0}" = "1" ]; then
  echo "RESET_DB=1 -> removing SQLite db at ${sqlite_path}"
  rm -f "$sqlite_path"

  echo "RESET_DB=1 -> removing seed markers ${seed_marker_glob}"
  rm -f $seed_marker_glob || true
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
# -----------------------------------------------------------------------------
if [ "$use_sqlite" = "1" ]; then
  echo "USE_SQLITE=1 -> running migrations on SQLite..."
  # Ensure Django sees SQLITE_PATH (settings.py must use SQLITE_PATH env var!)
  export SQLITE_PATH="$sqlite_path"
  python manage.py migrate --noinput
elif [ "$has_db_config" = "true" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput
else
  echo "No DATABASE_URL / POSTGRES_HOST set; attempting migrations with default DB..."
  python manage.py migrate --noinput
fi

# -----------------------------------------------------------------------------
# 2b) Background seeding (Strategy 1) with "seed-once" marker
#   - Avoid reseeding on container recycle by using a persistent marker file.
# -----------------------------------------------------------------------------
seed="${SEED_CONVERSATIONS:-1}"
seed_total="${SEED_N:-100}"
seed_batch="${SEED_BATCH:-5}"
seed_sleep="${SEED_SLEEP:-2}"

# Marker path must be on persistent storage too
seed_marker="${SEED_MARKER_PATH:-/home/site/.seed_done_${seed_total}}"

if [ "$use_sqlite" = "1" ] && [ "$seed" = "1" ]; then
  export SQLITE_PATH="$sqlite_path"

  if [ -f "$seed_marker" ]; then
    echo "Background seeding skipped: marker exists ($seed_marker)."
  else
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
marker = os.getenv("SEED_MARKER_PATH")

# Defensive: if marker exists (race) -> exit
if marker and os.path.exists(marker):
    print(f"[seed] marker exists -> skip ({marker})")
    raise SystemExit(0)

while True:
    current = SimulatedConversation.objects.count()
    if current >= total:
        print(f"[seed] done (current={current} >= total={total}).")
        break

    remaining = total - current
    n = batch if remaining > batch else remaining
    print(f"[seed] simulate_conversations --n {n} (current={current}, remaining={remaining})")
    call_command("simulate_conversations", n=n)

    time.sleep(sleep_s)

# Write marker only when target reached
if marker:
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"seeded_total={total}\n")
        print(f"[seed] marker written: {marker}")
    except Exception as e:
        print(f"[seed] WARN: could not write marker {marker}: {e!r}")
PY
    ) &
  fi
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