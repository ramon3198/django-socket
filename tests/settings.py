"""Settings minimos para la suite. Sin proyecto, sin servidor, sin migraciones."""

SECRET_KEY = "solo-para-tests"
DEBUG = True
ALLOWED_HOSTS = ["testserver", "miapp.com"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django_socket",
]

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}

USE_TZ = True
DJANGO_SOCKET = {}

STATIC_URL = "/static/"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]
