import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    BASE_URL = os.getenv("BASE_URL", "http://0.0.0.0:8001")

    ALJOUD_DB_PATH = os.getenv("ALJOUD_DB_PATH", os.path.join(os.getcwd(), "aljoud.db"))
    STORAGE_ROOT = os.getenv("STORAGE_ROOT", os.path.dirname(ALJOUD_DB_PATH))

    UPLOADS_DIR = os.getenv("UPLOADS_DIR", os.path.join(STORAGE_ROOT, "uploads"))
    BRANDING_DIR = os.getenv("BRANDING_DIR", os.path.join(STORAGE_ROOT, "branding"))
    EXPORTS_DIR = os.getenv("EXPORTS_DIR", os.path.join(STORAGE_ROOT, "exports"))
    BACKUPS_DIR = os.getenv("BACKUPS_DIR", os.path.join(STORAGE_ROOT, "backups"))
    LOGS_DIR = os.getenv("LOGS_DIR", os.path.join(STORAGE_ROOT, "logs"))

    AGENT_SHARED_SECRET = os.getenv("AGENT_SHARED_SECRET", "")

    DEFAULT_LOCALE = os.getenv("DEFAULT_LOCALE", "ar")
    DATE_FORMAT = os.getenv("DATE_FORMAT", "%d.%m.%Y")
    DATETIME_FORMAT = os.getenv("DATETIME_FORMAT", "%d.%m.%Y %H:%M")
