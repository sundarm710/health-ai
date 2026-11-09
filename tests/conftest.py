import os
import tempfile

# Tests that import the real FastAPI app must never write into the developer's real request log.
os.environ.setdefault("H2F_LOG_PATH", os.path.join(tempfile.mkdtemp(prefix="h2f-test-logs-"), "middleware.jsonl"))
