import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"
RUNTIME_DIR = Path(os.getenv("CLEARLY_RUNTIME_DIR", ROOT / "runtime"))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
DB_PATH = RUNTIME_DIR / "clearly.db"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
# auto = LLM when a key is present, otherwise OCR + rule-based parser
EXTRACTOR = os.getenv("EXTRACTOR", "auto")
# Small pause between stages so the live view is readable in a demo. 0 disables it.
STAGE_DELAY_MS = int(os.getenv("STAGE_DELAY_MS", "350"))

for d in (RUNTIME_DIR, UPLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)
