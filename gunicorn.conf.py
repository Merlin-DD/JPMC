"""Gunicorn configuration for Render.

Auto-loaded with no `-c` flag: gunicorn's `--config` setting defaults to
`./gunicorn.conf.py` in the process's working directory, and Render's
startCommand (`gunicorn synth_pnl.wsgi:application`, see render.yaml) runs
from the repo root — so this file takes effect without any change to
render.yaml.

preload_app=True: the app — and so Django's AppConfig.ready() — loads
once in the master process before any worker forks, then every worker
starts from that already-imported copy. Cheaper memory, faster worker
boot. The cost is that anything ready() does runs pre-fork, in the
master, which is why book/apps.py deliberately does nothing there but
register a signal (see its docstring, and book/startup.py). The database
bootstrap and the scheduler's background threads start below instead, in
post_fork, which runs once per worker, in that worker's own process,
strictly after its fork — the boundary SQLite requires and Python thread
state can't cross at all.
"""

preload_app = True


def post_fork(server, worker):
    from book import startup

    startup.ensure_started()
