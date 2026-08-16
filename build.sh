#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input

# Run unconditionally. `bootstrap` decides for itself whether there is
# anything to do, and is additive — it creates missing tables and fills
# empty ones, never dropping or rewriting populated data.
#
# The previous guard tested `[ ! -f "$DB_PATH" ]`. That looked reasonable
# but was wrong: SQLite creates an empty file on first connection, so as
# soon as the app had started once the file existed with no tables in it,
# every subsequent deploy skipped bootstrap and production served an empty
# database. The file's existence says nothing about the schema.
python manage.py bootstrap
python manage.py compute_attribution
python manage.py generate_commentary
