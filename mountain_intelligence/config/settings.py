"""Django settings.

Django is used only for the ORM, migrations, configuration and management commands.
No Django HTTP stack (admin, sessions, URLconf, WSGI/ASGI) is installed or served.
All configuration comes from environment variables; see `.env.example`.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env_str(name, default).split(",") if item.strip()]


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(env_str("MI_DATA_DIR", str(BASE_DIR / "data")))
FIXTURES_DIR = Path(env_str("MI_FIXTURES_DIR", str(BASE_DIR / "fixtures")))

MI_ENV = env_str("MI_ENV", "development")  # development | test | production
DEBUG = False  # Django debug pages are never served; keep it off everywhere.

# Nothing is signed with this key (no sessions, no cookies). A random per-process key is
# acceptable when none is provided; production may still pin one through the environment.
SECRET_KEY = env_str("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)

ALLOWED_HOSTS: list[str] = []  # Django does not serve HTTP.

INSTALLED_APPS = [
    "django.contrib.postgres",
    "mountain_intelligence.catalog",
    "mountain_intelligence.sources",
    "mountain_intelligence.ingestion",
    "mountain_intelligence.recommendations",
    "mountain_intelligence.offers",
]
MIDDLEWARE: list[str] = []
TEMPLATES: list[dict] = []

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env_str("POSTGRES_HOST", "localhost"),
        "PORT": env_int("POSTGRES_PORT", 5432),
        "NAME": env_str("POSTGRES_DB", "mountain_intelligence"),
        "USER": env_str("POSTGRES_USER", "mountain"),
        "PASSWORD": env_str("POSTGRES_PASSWORD"),
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            # Bound every statement; MCP queries are read-only and must stay fast.
            "options": f"-c statement_timeout={env_int('DB_STATEMENT_TIMEOUT_MS', 15000)}",
            "connect_timeout": 5,
        },
        "TEST": {"NAME": env_str("POSTGRES_TEST_DB", "test_mountain_intelligence")},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env_str("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "mi",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"
USE_I18N = False

# --- Celery -----------------------------------------------------------------------------
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", REDIS_URL or "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = None  # results are persisted as IngestionRun rows, not in Celery
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 3600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT", 3300)
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# --- Catalog / domain ---------------------------------------------------------------------
# Synthetic demo fixtures are excluded from every MCP answer unless explicitly enabled.
MI_INCLUDE_SYNTHETIC_DATA = env_bool("MI_INCLUDE_SYNTHETIC_DATA", False)
MI_STALE_AFTER_DAYS = env_int("MI_STALE_AFTER_DAYS", 120)
MI_OFFER_MAX_AGE_DAYS = env_int("MI_OFFER_MAX_AGE_DAYS", 7)

# --- Ingestion ----------------------------------------------------------------------------
INGESTION_SCHEDULE_ENABLED = env_bool("INGESTION_SCHEDULE_ENABLED", False)
INGESTION_SCHEDULER_INTERVAL_SECONDS = env_int("INGESTION_SCHEDULER_INTERVAL_SECONDS", 900)
INGESTION_USER_AGENT = env_str(
    "INGESTION_USER_AGENT", "MountainIntelligenceBot/0.1 (+https://github.com/mountain-intelligence-mcp)"
)
INGESTION_TIMEOUT_SECONDS = float(env_str("INGESTION_TIMEOUT_SECONDS", "20"))
INGESTION_MAX_RESPONSE_BYTES = env_int("INGESTION_MAX_RESPONSE_BYTES", 5 * 1024 * 1024)
INGESTION_MAX_REDIRECTS = env_int("INGESTION_MAX_REDIRECTS", 5)
INGESTION_MAX_RETRIES = env_int("INGESTION_MAX_RETRIES", 3)
INGESTION_MIN_DELAY_SECONDS = float(env_str("INGESTION_MIN_DELAY_SECONDS", "2.0"))
INGESTION_LOCK_TTL_SECONDS = env_int("INGESTION_LOCK_TTL_SECONDS", 4 * 3600)
INGESTION_RESPECT_ROBOTS_TXT = env_bool("INGESTION_RESPECT_ROBOTS_TXT", True)

# --- MCP server ---------------------------------------------------------------------------
MCP_SERVER_NAME = "mountain-intelligence"
MCP_HTTP_PATH = env_str("MCP_HTTP_PATH", "/mcp")
MCP_AUTH_TOKENS = env_list("MCP_AUTH_TOKENS")
MCP_ALLOW_UNAUTHENTICATED = env_bool("MCP_ALLOW_UNAUTHENTICATED", False)
MCP_ALLOWED_HOSTS = env_list("MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*")
MCP_ALLOWED_ORIGINS = env_list("MCP_ALLOWED_ORIGINS")
MCP_RATE_LIMIT_PER_MINUTE = env_int("MCP_RATE_LIMIT_PER_MINUTE", 120)
MCP_MAX_REQUEST_BODY_BYTES = env_int("MCP_MAX_REQUEST_BODY_BYTES", 256 * 1024)

# --- Logging (always stderr: stdout is reserved for the stdio MCP transport) ---------------
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "mountain_intelligence.ops.logging.JsonFormatter"}},
    "handlers": {"stderr": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "json"}},
    "root": {"handlers": ["stderr"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "httpx2": {"level": "WARNING"},
        "httpcore2": {"level": "WARNING"},
    },
}
