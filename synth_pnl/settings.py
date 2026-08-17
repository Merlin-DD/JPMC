import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "")

DEBUG = os.environ.get("DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
ALLOWED_HOSTS += ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "book",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "synth_pnl.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "synth_pnl.wsgi.application"

DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "book.sqlite3"))

# Background refresh cadences, in seconds. They live here rather than in
# book/scheduler.py because the request path needs REFRESH_SECONDS (to
# set the client poll interval and the staleness thresholds) and must not
# import the scheduler — and through it, yfinance — to learn it.
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))
COMMENTARY_SECONDS = int(os.environ.get("COMMENTARY_SECONDS", "900"))

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DB_PATH,
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Without this, unhandled request exceptions vanish in production.
#
# Django's built-in DEFAULT_LOGGING only sends the 'django.request' logger
# to two handlers: 'console', which is filtered by require_debug_true (so
# it does nothing at all once DEBUG=False), and 'mail_admins', which is a
# silent no-op unless ADMINS/EMAIL_BACKEND are configured (they aren't
# here). The result: every 500 is fully swallowed — nothing on stdout,
# nothing anywhere — leaving only the access-log line WSGI/gunicorn prints
# regardless. This LOGGING block replaces the 'console' handler with one
# that always runs, points 'django.request' at ERROR to it explicitly (own
# handler, not through 'django' -> 'mail_admins'), and gives every other
# logger — including our own book.* modules — a root handler at INFO so
# nothing has to opt in individually.
#
# No extra work is needed to get full tracebacks: Django's own request
# logging already calls logger.error(..., exc_info=...) for a 500, and the
# stdlib's logging.Formatter renders exc_info into the output automatically
# whenever it's present, for any handler.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            # Otherwise this also bubbles up through 'django' to the root
            # logger's 'console' handler and prints every 500 twice.
            "propagate": False,
        },
    },
}
