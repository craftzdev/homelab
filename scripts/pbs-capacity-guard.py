#!/usr/bin/env python3
"""vzdump hook: retain enough free PBS space for the next backup batch.

The batch currently grows by about 49 GiB across all three PVE nodes. Require
80 GiB before starting the daily batch, and 16 GiB before each individual VM.
Failing the hook reports a failed backup task instead of filling the PBS OS
filesystem. This is a guard, not a substitute for pruning and scheduled GC.
"""

import json
import os
import socket
import subprocess
import sys

GIB = 1024**3
MIN_FREE = {"job-start": 80 * GIB, "backup-start": 16 * GIB}
STORAGE = "pbs-gateway"


def check_status(status, minimum):
    if not isinstance(status, dict):
        raise ValueError("PBS status is not a JSON object")
    if status.get("active") != 1 or status.get("enabled") != 1:
        raise ValueError("PBS storage is not active and enabled")
    if status.get("type") != "pbs":
        raise ValueError("Storage is not PBS")
    available = status.get("avail")
    if type(available) is not int or available < 0:
        raise ValueError("PBS did not return a valid free-space value")
    if available < minimum:
        raise ValueError(
            f"Only {available / GIB:.1f} GiB free; {minimum / GIB:.0f} GiB required. "
            "Backup stopped before writing. Check prune and garbage collection."
        )
    return available


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase not in MIN_FREE or os.environ.get("STOREID") != STORAGE:
        return 0
    try:
        result = subprocess.run(
            ["pvesh", "get", f"/nodes/{socket.gethostname()}/storage/{STORAGE}/status",
             "--output-format", "json"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        available = check_status(json.loads(result.stdout), MIN_FREE[phase])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"PBS CAPACITY GUARD ({phase}): {exc}", file=sys.stderr)
        return 1
    print(f"PBS CAPACITY GUARD ({phase}): {available / GIB:.1f} GiB available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
