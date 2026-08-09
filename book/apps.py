from django.apps import AppConfig


class BookConfig(AppConfig):
    name = "book"

    def ready(self):
        from book import scheduler

        scheduler.start()
