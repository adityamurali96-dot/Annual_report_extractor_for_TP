import os
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Claude API (used for standalone page identification only)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# App settings
MAX_UPLOAD_SIZE_MB = 50

# Server
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8000))
