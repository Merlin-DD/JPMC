from django.apps import AppConfig


class BookConfig(AppConfig):
    name = "book"

    def ready(self):
        from book import scheduler, startup

        # Bootstrap before the scheduler starts: the scheduler's own
        # missing-schema check exists for the case this fails, not as a
        # substitute for running it first.
        startup.run()
        scheduler.start()
