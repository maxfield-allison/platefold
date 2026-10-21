"""Local readiness probe with the same explicit Host allowlist as browser requests."""

import os
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8787/health/ready",
    headers={"Host": os.environ.get("PLATEFOLD_HEALTH_HOST", "127.0.0.1:8787")},
)
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
