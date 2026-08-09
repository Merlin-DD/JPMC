#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input

DB_FILE="${DB_PATH:-./book.sqlite3}"

has_cmd() { python manage.py help --commands | grep -qx "$1"; }

if [ ! -f "$DB_FILE" ]; then
  echo "No DB at $DB_FILE — bootstrapping."
  has_cmd seed                && python manage.py seed                || echo "skip: seed not implemented yet"
  has_cmd compute_attribution && python manage.py compute_attribution || echo "skip: compute_attribution not implemented yet"
  has_cmd generate_commentary && python manage.py generate_commentary || echo "skip: generate_commentary not implemented yet"
else
  echo "DB exists at $DB_FILE — skipping bootstrap."
fi