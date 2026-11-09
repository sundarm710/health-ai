import os
import tempfile

# Tests that import the real FastAPI app must never write into the developer's real request log.
os.environ.setdefault("FHIRLAB_LOG_PATH", os.path.join(tempfile.mkdtemp(prefix="fhirlab-test-logs-"), "middleware.jsonl"))
