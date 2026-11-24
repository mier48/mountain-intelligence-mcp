"""Mountain Intelligence MCP: technical mountain-equipment catalog served over MCP."""

__version__ = "0.1.0"


def setup_django() -> None:
    """Initialise Django for non-manage.py entrypoints (MCP server, scripts)."""
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mountain_intelligence.config.settings")
    django.setup()
