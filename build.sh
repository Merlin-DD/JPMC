#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

DB_PATH="${DB_PATH:-./book.sqlite3}"

if [ ! -f "$DB_PATH" ]; then
  python manage.py seed
  python manage.py compute
  python manage.py commentary
fi
