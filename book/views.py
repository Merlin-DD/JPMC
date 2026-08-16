from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from book.dashboard import build_summary
from book.db import get_conn

PALETTE_COOKIE = "palette"
PALETTES = ("standard", "cvd")
DEFAULT_PALETTE = "standard"


def _palette(request) -> str:
    """Read the palette server-side so the correct one is in the initial
    HTML. Doing this in JS after load would flash the wrong colours."""
    choice = request.COOKIES.get(PALETTE_COOKIE, DEFAULT_PALETTE)
    return choice if choice in PALETTES else DEFAULT_PALETTE


def _summary():
    conn = get_conn()
    try:
        return build_summary(conn, settings.REFRESH_SECONDS)
    finally:
        conn.close()


def _page(request, template, active_page):
    """Shared context for every page that carries the desk header."""
    return render(
        request,
        template,
        {
            "summary": _summary(),
            "palette": _palette(request),
            "active_page": active_page,
        },
    )


def index(request):
    return _page(request, "book/dashboard.html", "book")


def risk(request):
    return _page(request, "book/risk.html", "risk")


def api_summary(request):
    """Same read model as `/`, for the auto-refresh poll."""
    return JsonResponse(_summary())


def healthz(request):
    # Liveness probe: must succeed even when ingestion is broken, so it
    # deliberately touches neither the database nor any external API.
    return HttpResponse("ok", content_type="text/plain")
