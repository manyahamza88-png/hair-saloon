"""
Django settings for the hair salon booking system.

Designed to run on PythonAnywhere with no code changes: everything that differs
between your laptop and the server is read from environment variables (or from
a .env file sitting next to manage.py).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Tiny .env loader (no external dependency, works fine on PythonAnywhere)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return str(env(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in str(env(name, default)).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,.pythonanywhere.com")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "https://*.pythonanywhere.com")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "booking",
    "chat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# WhiteNoise is optional: drop it silently if it is not installed so the
# project still boots on a bare environment.
try:  # pragma: no cover - trivial import guard
    import whitenoise  # noqa: F401
except ImportError:  # pragma: no cover
    MIDDLEWARE.remove("whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "hairsaloon.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "booking.context_processors.salon",
            ],
        },
    },
]

WSGI_APPLICATION = "hairsaloon.wsgi.application"


# ---------------------------------------------------------------------------
# Database: SQLite by default, MySQL on PythonAnywhere if you set DB_NAME
# ---------------------------------------------------------------------------
if env("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": env("DB_ENGINE", "django.db.backends.mysql"),
            "NAME": env("DB_NAME"),
            "USER": env("DB_USER", ""),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", ""),
            "PORT": env("DB_PORT", ""),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

_use_whitenoise_storage = (
    "whitenoise.middleware.WhiteNoiseMiddleware" in MIDDLEWARE and not DEBUG
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if _use_whitenoise_storage
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
if env("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
    EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "20"))
else:
    # Prints emails to the console: handy for local development.
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Hair Salon <no-reply@example.com>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Absolute base URL used inside emails (accept / decline links).
SITE_BASE_URL = env("SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# How long an accept / decline link stays valid, in days.
DECISION_LINK_MAX_AGE_DAYS = int(env("DECISION_LINK_MAX_AGE_DAYS", "30"))


# ---------------------------------------------------------------------------
# Google OAuth (only needed for the "connect my Google account" flow)
# ---------------------------------------------------------------------------
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", "")


# ---------------------------------------------------------------------------
# Auth redirects
# ---------------------------------------------------------------------------
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/manage/"
LOGOUT_REDIRECT_URL = "/"


# ---------------------------------------------------------------------------
# Security (only bites when DEBUG is off)
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    X_FRAME_OPTIONS = "DENY"

    # Off by default because they are only safe once the site really is
    # HTTPS-only. On PythonAnywhere, tick "Force HTTPS" on the Web tab and then
    # turn these on -- HSTS in particular is hard to undo, so raise it in steps
    # (60 -> 3600 -> 31536000) once you are sure.
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "0"))
    if SECURE_HSTS_SECONDS:
        SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
        SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "booking": {
            "handlers": ["console"],
            "level": env("BOOKING_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "googleapiclient": {"level": "WARNING"},
    },
}
