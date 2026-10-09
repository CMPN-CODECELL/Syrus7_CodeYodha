"""Stops every Countersign process started by scripts/dev.py (Windows and POSIX)."""

import os
import subprocess
import sys

PATTERNS = ["countersign.broker.app", "countersign.feed.main", "countersign.executor.main", "countersign.api.main", "countersign.monitor.main", "scripts/dev.py", "scripts\\dev.py"]


def main() -> None:
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -ne $null -and ("
            + " -or ".join(f"$_.CommandLine -like '*{p}*'" for p in PATTERNS)
            + ") } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        print("stopped:", out.stdout.split())
        if "--keep-web" in sys.argv:
            return
        web = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; $_.OwningProcess }"], capture_output=True, text=True)
        if web.stdout.strip():
            print("stopped web:", web.stdout.split())
    else:
        for p in PATTERNS:
            subprocess.run(["pkill", "-f", p])


if __name__ == "__main__":
    main()
