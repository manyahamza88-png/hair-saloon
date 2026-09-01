#!/usr/bin/env bash
#
# deploy.sh — Hair Salon deployment for PythonAnywhere. Runs the post-pull steps
# from DEPLOY_PYTHONANYWHERE.md. Safe to re-run.
#
# Usage:
#   bash deploy.sh                 # install deps, migrate, collectstatic, check, reload
#   bash deploy.sh --pull          # also `git pull` first
#   bash deploy.sh --test          # also run the test suite before reloading
#   bash deploy.sh --no-reload     # do everything except touch the WSGI file
#
# Environment overrides:
#   PYTHON=python3.11              # interpreter (default: autodetected)
#   WSGI_FILE=/var/www/...wsgi.py  # web app file to touch for reload
#
# Guarantees:
#   * Never runs plain `makemigrations` (only `migrate`); migrations are
#     authored and committed locally.
#   * Never deletes or overwrites db.sqlite3 or the media/ folder.
#   * Never rotates DJANGO_SECRET_KEY or edits .env — rotating the key would
#     invalidate the stored Google tokens and chat secrets.

set -euo pipefail

DO_PULL=0
DO_RELOAD=1
DO_TEST=0

for arg in "$@"; do
    case "$arg" in
        --pull)       DO_PULL=1 ;;
        --no-reload)  DO_RELOAD=0 ;;
        --test)       DO_TEST=1 ;;
        -h|--help)    awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# Always operate from this script's directory (the project root), whatever the CWD.
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

warn() { echo "WARNING: $*" >&2; }
step() { echo; echo "==> $*"; }

echo "=========================================="
echo " Hair Salon deploy — $PROJECT_DIR"
echo "=========================================="

if [ ! -f manage.py ]; then
    echo "ERROR: manage.py not found here. Run this from the project checkout." >&2
    exit 1
fi

# --- 0. pick an interpreter ---------------------------------------------------
# Version mismatch between the console and the Web tab is the classic
# PythonAnywhere failure, so say out loud which one we are using.
if [ -z "${PYTHON:-}" ]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "${PYTHON:-}" ] || ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: no Python interpreter found. Set PYTHON=python3.11 and retry." >&2
    exit 1
fi
echo "Using interpreter: $PYTHON ($("$PYTHON" --version 2>&1))"
echo "Make sure the Web tab is set to the SAME Python version."

# --- 1. optional git pull -----------------------------------------------------
if [ "$DO_PULL" -eq 1 ]; then
    step "git pull"
    git pull
fi
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Deploying commit: $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
fi

# --- 2. environment sanity ----------------------------------------------------
step "Checking configuration"
if [ ! -f .env ]; then
    warn "No .env file. Copy .env.example to .env and fill it in, or the site will"
    warn "run with DEBUG on and an insecure SECRET_KEY."
else
    if grep -qE '^DJANGO_SECRET_KEY=(dev-only-insecure-key-change-me|change-me)' .env; then
        warn "DJANGO_SECRET_KEY is still the example value. Generate a real one:"
        warn "  $PYTHON -c \"from django.core.management.utils import get_random_secret_key as k; print(k())\""
        warn "Note: changing it later invalidates stored Google tokens (just reconnect)."
    fi
    if grep -qE '^DJANGO_DEBUG=(True|true|1|yes|on)' .env; then
        warn "DJANGO_DEBUG is on. Turn it off before serving real customers."
    fi
    if ! grep -q '^SITE_BASE_URL=' .env; then
        warn "SITE_BASE_URL is not set. Accept/decline links in emails will point at localhost."
    fi
fi

# --- 3. dependencies ----------------------------------------------------------
step "Installing dependencies from requirements.txt"
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo "    (virtualenv: $VIRTUAL_ENV)"
    "$PYTHON" -m pip install -q -r requirements.txt
else
    # PythonAnywhere's system Python is read-only, so --user is required.
    "$PYTHON" -m pip install -q --user -r requirements.txt
fi

# --- 4. migration-drift warning (read-only) -----------------------------------
# Migrations are never generated on the server; just warn if the models have
# drifted from what is committed, which means someone forgot to commit one.
if ! "$PYTHON" manage.py makemigrations --check --dry-run >/dev/null 2>&1; then
    warn "Models differ from the committed migrations. Author and commit the"
    warn "migration locally; do NOT run makemigrations on the server."
fi

# --- 5. migrate (never makemigrations) ----------------------------------------
step "Applying database migrations"
if ! "$PYTHON" manage.py migrate --noinput; then
    echo "ERROR: migrate failed." >&2
    echo "       If it reports 'Conflicting migrations detected', run once:" >&2
    echo "         $PYTHON manage.py makemigrations --merge" >&2
    echo "       then re-run this script. Do NOT run plain makemigrations." >&2
    exit 1
fi

# --- 6. static files ----------------------------------------------------------
step "Collecting static files"
"$PYTHON" manage.py collectstatic --noinput

# --- 7. system checks ---------------------------------------------------------
step "Running Django system checks"
"$PYTHON" manage.py check

# --- 8. optional test suite ---------------------------------------------------
if [ "$DO_TEST" -eq 1 ]; then
    step "Running the test suite"
    DJANGO_DEBUG=True "$PYTHON" manage.py test --noinput
fi

# --- 9. reload the web app ----------------------------------------------------
if [ "$DO_RELOAD" -eq 1 ]; then
    if [ -z "${WSGI_FILE:-}" ] || [ ! -e "${WSGI_FILE:-}" ]; then
        detected="$(ls /var/www/*_wsgi.py 2>/dev/null || true)"
        if [ "$(printf '%s\n' "$detected" | grep -c .)" = "1" ]; then
            WSGI_FILE="$detected"
        fi
    fi
    if [ -n "${WSGI_FILE:-}" ] && [ -e "$WSGI_FILE" ]; then
        step "Reloading the web app (touch $WSGI_FILE)"
        touch "$WSGI_FILE"
    else
        step "Could not reload automatically"
        warn "WSGI file not found (looked for ${WSGI_FILE:-/var/www/*_wsgi.py})."
        warn "Hit Reload on the Web tab, or set WSGI_FILE=/var/www/<domain>_wsgi.py"
    fi
fi

# --- 10. what to check next ---------------------------------------------------
cat <<'NEXT'

==> Done.

Worth checking after a deploy:
  * Open the site and make one test booking.
  * Google still linked?    manage.py check_google
                            (or the Google setup page at /manage/google/)
  * Live chat transcripts are only purged if the daily task exists:
        cd ~/hair-saloon && python3.11 manage.py purge_old_chats
    Add it on the PythonAnywhere "Tasks" tab if you have chat switched on.

First deploy only:
        manage.py init_salon --name "Your Salon" --timezone Europe/Berlin
        manage.py createsuperuser
NEXT
