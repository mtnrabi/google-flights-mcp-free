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
# The fair-use suite's arithmetic is written against 150/2,000 -- it spends
# 120 searches and checks the 121st, and rewriting those loops would test
# nothing new. The SHIPPED defaults are 50/250 since 2026-09-17, matching
# what production has enforced since 09-15; `test_signin.py`'s
# `test_the_defaults_ship_as_ten_a_day` clears these two and asserts that.
os.environ.setdefault("FAIR_USE_DAY_CAP", "150")
os.environ.setdefault("FAIR_USE_MONTH_CAP", "2000")
