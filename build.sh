#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input

# Database bootstrap (schema, seed CSVs, first attribution + commentary
# pass) deliberately does NOT happen here. On Render the persistent disk
# is mounted at runtime, not during the build step, so DB_PATH doesn't
# exist to be opened yet — it happens at application startup instead. See
# book/startup.py, run from book/apps.py's ready(). `python manage.py
# bootstrap` remains available for manual use.
