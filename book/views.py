from django.http import HttpResponse


def index(request):
    return HttpResponse("OK", content_type="text/plain")


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
