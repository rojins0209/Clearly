import os
import tempfile

# Must be set before the app modules are imported
os.environ.setdefault("CLEARLY_RUNTIME_DIR", tempfile.mkdtemp(prefix="clearly-test-"))
os.environ["STAGE_DELAY_MS"] = "0"
os.environ["CLEARLY_TODAY"] = "2026-11-10"
os.environ["EXTRACTOR"] = "rules"   # deterministic; LLM path is exercised manually / in the demo

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture
def fresh_db():
    db.reset()
    yield
    db.reset()
