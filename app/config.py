from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv(Path(__file__).with_name(".env"))

MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Set MONGODB_URI in app/.env before starting the API.")

ALLOWED_EXTENSIONS = [".pdf", ".txt"]
MAX_FILE_SIZE_MB = 10
UPLOAD_DIR = "uploads"
GEMINI_API_KEY = os.getenv("GOOGLE_AI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Tried in order after GEMINI_MODEL when a model is overloaded, unavailable, or too slow.
GEMINI_FALLBACK_MODELS = [model.strip() for model in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash").split(",") if model.strip()]

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30


def validate_auth_config():
    if len(JWT_SECRET_KEY.encode("utf-8")) < 32:
        raise RuntimeError("Set JWT_SECRET_KEY to a random secret of at least 32 bytes in app/.env.")
