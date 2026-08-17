from django.apps import AppConfig
from django.core.signals import request_started


def _on_first_request(sender, **kwargs):
    """Fallback trigger for `manage.py runserver`, which never forks and
    has no post_fork hook — see book/startup.py. Disconnects itself so
    later requests don't pay even the cost of checking the (already
    idempotent) flag in ensure_started()."""
    from book import startup

    request_started.disconnect(_on_first_request, dispatch_uid="book.startup")
    startup.ensure_started()


class BookConfig(AppConfig):
    name = "book"

    def ready(self):
        """Registers a trigger only — never opens a database connection
        or starts a thread here.

        Render's gunicorn.conf.py sets preload_app=True, so this runs in
        the gunicorn master process before any worker is forked. SQLite
        connections and Python threads must never be carried across
        fork() (see book/startup.py for what goes wrong when they are).
        The real work happens post-fork: primarily via gunicorn.conf.py's
        post_fork hook, with the request_started connection below as a
        fallback for `manage.py runserver`, which never forks at all.
        """
        request_started.connect(_on_first_request, dispatch_uid="book.startup")
