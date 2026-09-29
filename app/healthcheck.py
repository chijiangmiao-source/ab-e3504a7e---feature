"""Container healthcheck: exit 0 iff the service answers /health."""

import os
import sys
import urllib.request


def main():
    port = os.environ.get("PORT", "8080")
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
