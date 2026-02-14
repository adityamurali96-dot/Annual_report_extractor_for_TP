import logging
import os
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Claude API (used for standalone page identification only)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Warn if key looks invalid (Anthropic keys start with "sk-ant-")
if ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-"):
    logging.getLogger(__name__).warning(
        "ANTHROPIC_API_KEY doesn't start with 'sk-ant-'. "
        "Claude page identification may fail."
    )

# Extraction settings (all configurable via environment)
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "50"))
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "500"))
OCR_DPI = int(os.environ.get("OCR_DPI", "150"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
CLEANUP_AGE_SECONDS = int(os.environ.get("CLEANUP_AGE_SECONDS", "3600"))

# Server
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))
