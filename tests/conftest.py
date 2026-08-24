import os
import sys
from pathlib import Path

# Tests import `src.*`, so the package root has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Settings are read at load_settings() time. Give every test a valid baseline
# so importing the server never depends on a developer's real .env.
os.environ.setdefault("BASE_LAMBDA_URL", "https://lambda.test.invalid")
os.environ.setdefault("RAPID_AUTH", "test-secret")
os.environ.setdefault("MCP_PUBLIC_URL", "https://mcp.test.invalid/mcp")
os.environ.setdefault("ADS_ENABLED", "false")
