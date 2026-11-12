#!/usr/bin/env bash
# Bring up a throw-away Galaxy 26.1 on PostgreSQL for byoc-sync's end-to-end test.
#
#   testenv/setup.sh up      start PostgreSQL (docker, no volumes) and Galaxy (background)
#   testenv/setup.sh seed [phase1|phase2]   create test users and fill in their BYOC form (see seed_users.py)
#   testenv/setup.sh admin-key   print an API key of a real admin account (what byoc-sync needs)
#   testenv/setup.sh url         print the Galaxy URL (port from GALAXY_PORT, default 8089)
#   testenv/setup.sh down    stop Galaxy and remove the PostgreSQL container
#
# Needs: docker, git, curl and uv.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
GALAXY_SOURCE=${GALAXY_SOURCE:-https://github.com/galaxyproject/galaxy}
GALAXY_REF=${GALAXY_REF:-release_26.1}
GALAXY_PORT=${GALAXY_PORT:-8089}
PG_PORT=${PG_PORT:-55432}
ADMIN_KEY=${ADMIN_KEY:-byoc-sync-testenv-admin-key}
PG_CONTAINER=${PG_CONTAINER:-byoc-sync-pg}
galaxy="$here/galaxy"

up() {
  if curl -sf "http://127.0.0.1:$GALAXY_PORT/api/version" >/dev/null 2>&1; then
    echo "Galaxy is already running on http://127.0.0.1:$GALAXY_PORT"
    return
  fi
  if ! docker inspect "$PG_CONTAINER" >/dev/null 2>&1; then
    docker run -d --name "$PG_CONTAINER" -e POSTGRES_USER=galaxy -e POSTGRES_PASSWORD=galaxy \
      -e POSTGRES_DB=galaxy -p "127.0.0.1:$PG_PORT:5432" postgres:16-alpine >/dev/null
  fi
  if [[ ! -d "$galaxy" ]]; then
    git clone -q --depth 1 --branch "$GALAXY_REF" "$GALAXY_SOURCE" "$galaxy"
  fi
  # Pre-create Galaxy's virtualenv with uv; otherwise Galaxy's startup may create a conda environment in
  # the user's own conda installation.
  if [[ ! -d "$galaxy/.venv" ]]; then
    uv venv -q --seed -p 3.12 "$galaxy/.venv"
  fi
  mkdir -p "$here/state"
  if [[ ! -f "$here/state/vault_conf.yml" ]]; then
    key=$("$galaxy/.venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    printf 'type: database\npath_prefix: /galaxy\nencryption_keys:\n  - %s\n' "$key" > "$here/state/vault_conf.yml"
  fi
  sed -e "s|@GALAXY_ROOT@|$galaxy|" -e "s|@GALAXY_PORT@|$GALAXY_PORT|" -e "s|@PG_PORT@|$PG_PORT|" \
      -e "s|@ADMIN_KEY@|$ADMIN_KEY|" -e "s|@TESTENV@|$here|" "$here/galaxy.yml.tmpl" > "$galaxy/config/galaxy.yml"
  (cd "$galaxy" && GALAXY_INSTALL_PREBUILT_CLIENT=1 nohup ./run.sh > "$here/galaxy.log" 2>&1 &)
  echo "Galaxy starting on http://127.0.0.1:$GALAXY_PORT (log: $here/galaxy.log)"
  for _ in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:$GALAXY_PORT/api/version" >/dev/null; then echo "Galaxy is up"; return; fi
    sleep 10
  done
  echo "Galaxy did not come up; see $here/galaxy.log" >&2
  exit 1
}

down() {
  (cd "$galaxy" && ./.venv/bin/galaxyctl stop 2>/dev/null) || pkill -f "$galaxy/.venv/bin/gunicorn" || true
  docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
}

case "${1:-}" in
  up) up ;;
  url) echo "http://127.0.0.1:$GALAXY_PORT" ;;
  admin-key) GALAXY_URL="http://127.0.0.1:$GALAXY_PORT" ADMIN_KEY="$ADMIN_KEY" "${PYTHON:-python3}" "$here/seed_users.py" admin-key ;;
  seed) GALAXY_URL="http://127.0.0.1:$GALAXY_PORT" ADMIN_KEY="$ADMIN_KEY" "${PYTHON:-python3}" "$here/seed_users.py" "${2:-phase1}" ;;
  down) down ;;
  *) echo "usage: $0 up|seed [phase1|phase2]|admin-key|url|down" >&2; exit 2 ;;
esac
