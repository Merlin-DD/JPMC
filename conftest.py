# Present so pytest puts the repo root on sys.path, letting tests import
# `book.attribution` directly. Nothing else belongs here — the attribution
# tests need no Django setup, since book/attribution.py imports no Django.
