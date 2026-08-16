from django.urls import path

from book import views

urlpatterns = [
    path("", views.index),
    path("risk", views.risk),
    path("api/summary", views.api_summary),
    path("healthz", views.healthz),
]
