from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_categories(sender: AppConfig, **kwargs: object) -> None:
    from .services import sync_categories

    sync_categories(using=str(kwargs.get("using", "default")))


class CatalogConfig(AppConfig):
    name = "mountain_intelligence.catalog"
    label = "catalog"

    def ready(self) -> None:
        post_migrate.connect(_sync_categories, sender=self, dispatch_uid="catalog_sync_categories")
