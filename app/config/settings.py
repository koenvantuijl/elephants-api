import os
from pathlib import Path

# =============================================================================
# Base paths
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================================
# Security / environment
# =============================================================================
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key-change-this")
DEBUG = os.environ.get("DEBUG", "0") == "1"

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    os.environ.get("WEBSITE_HOSTNAME", ""),
    "elephants-api-webapp-c2gzfjezdrgdf0g8.westeurope-01.azurewebsites.net",
]
ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]

# =============================================================================
# Applications
# =============================================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "chatbot",
]


# =============================================================================
# Middleware
# =============================================================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serveert /static in een gunicorn-only setup (zonder Nginx)
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# =============================================================================
# URL / WSGI / ASGI
# =============================================================================
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# =============================================================================
# Templates
# =============================================================================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# =============================================================================
# Database (Postgres)
# =============================================================================
if os.getenv("USE_SQLITE", "0") == "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "/tmp/db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB"),
            "USER": os.getenv("POSTGRES_USER"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD"),
            "HOST": os.getenv("POSTGRES_HOST"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }

# =============================================================================
# Internationalization
# =============================================================================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# =============================================================================
# Static files
# =============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise: zorgt dat admin CSS/JS correct geserveerd wordt vanuit STATIC_ROOT
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# (optioneel) extra WhiteNoise tuning; niet strikt noodzakelijk
WHITENOISE_MAX_AGE = 31536000  # 1 jaar caching voor immutable assets

# =============================================================================
# Default primary key field type
# =============================================================================
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# =============================================================================
# (Optioneel) CSRF trusted origins voor HTTPS hostnames (zet pas aan als nodig)
# =============================================================================
# Als je straks op Azure draait via https://<app>.azurewebsites.net, voeg dan toe:
# CSRF_TRUSTED_ORIGINS = [
#     f"https://{os.environ.get('WEBSITE_HOSTNAME')}" if os.environ.get("WEBSITE_HOSTNAME") else ""
# ]
# CSRF_TRUSTED_ORIGINS = [o for o in CSRF_TRUSTED_ORIGINS if o]
