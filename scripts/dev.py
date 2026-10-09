"""Starts the complete Countersign stack locally (100% free, no paid services).

Credential zones are enforced at process launch: each process receives only the broker keys it may hold.
  broker   : registers all key hashes (it is the "external" broker that issued them)
  executor : WRITE key only (the only process that can place/modify/cancel)
  api      : READ key + demo admin key + sandbox-only naive key; never the write key (it refuses to start if given one)
  monitor  : AUDIT (read-all) key only
  feed     : no keys
Usage:  uv run --project backend python scripts/dev.py [--no-web] [--no-docker]"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WEB = ROOT / "web"
SECRETS = ROOT / ".secrets"
LOGS = ROOT / ".logs"


def keys() -> dict:
    SECRETS.mkdir(exist_ok=True)
    p = SECRETS / "broker_keys.json"
    if not p.exists():
        p.write_text(json.dumps({k: "cs_" + k + "_" + secrets.token_urlsafe(24) for k in ("read", "write", "admin", "naive", "audit")}, indent=2))
    return json.loads(p.read_text())


def py() -> str:
    win = BACKEND / ".venv" / "Scripts" / "python.exe"
    nix = BACKEND / ".venv" / "bin" / "python"
    return str(win if win.exists() else nix if nix.exists() else sys.executable)


def wait_port(port: int, timeout: float = 60) -> bool:
    import socket

    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.4)
    return False


def main() -> None:
    args = set(sys.argv[1:])
    k = keys()
    LOGS.mkdir(exist_ok=True)
    base = {kk: v for kk, v in os.environ.items() if not kk.startswith("CS_BROKER_")}
    base["PYTHONIOENCODING"] = "utf-8"
    base["PYTHONUNBUFFERED"] = "1"

    if "--no-docker" not in args:
        print("starting postgres + valkey (docker compose)...")
        subprocess.run(["docker", "compose", "up", "-d", "postgres", "valkey"], cwd=ROOT, check=True)
        wait_port(55432)
        wait_port(56379)
        time.sleep(1.0)
    print("bootstrapping schema + seed...")
    subprocess.run([py(), "-m", "countersign.core.bootstrap"], cwd=BACKEND, env=base, check=True)

    plan = [
        ("broker", ["-m", "countersign.broker.app"], {"CS_BROKER_READ_KEY": k["read"], "CS_BROKER_WRITE_KEY": k["write"], "CS_BROKER_ADMIN_KEY": k["admin"], "CS_BROKER_NAIVE_KEY": k["naive"], "CS_BROKER_AUDIT_KEY": k["audit"]}, 8100),
        ("feed", ["-m", "countersign.feed.main"], {}, None),
        ("executor", ["-m", "countersign.executor.main"], {"CS_BROKER_WRITE_KEY": k["write"]}, None),
        ("api", ["-m", "countersign.api.main"], {"CS_BROKER_READ_KEY": k["read"], "CS_BROKER_ADMIN_KEY": k["admin"], "CS_BROKER_NAIVE_KEY": k["naive"]}, 8000),
        ("monitor", ["-m", "countersign.monitor.main"], {"CS_BROKER_AUDIT_KEY": k["audit"]}, None),
    ]
    procs: list[tuple[str, subprocess.Popen]] = []
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    for name, argv, env_extra, port in plan:
        log = open(LOGS / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen([py(), *argv], cwd=BACKEND, env={**base, **env_extra}, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        procs.append((name, p))
        print(f"  {name:9s} pid {p.pid}  (keys: {', '.join(x.split('_')[2].lower() for x in env_extra) or 'none'})")
        if port:
            if not wait_port(port, 90):
                print(f"  ! {name} did not open port {port}; see .logs/{name}.log")
    if "--no-web" not in args and (WEB / "package.json").exists():
        log = open(LOGS / "web.log", "w", encoding="utf-8")
        # `pnpm dev` binds Next.js to 127.0.0.1 only (see web/package.json), so the app is not reachable from the network.
        p = subprocess.Popen("pnpm dev", cwd=WEB, env={**base, "BACKEND_URL": "http://127.0.0.1:8000"}, stdout=log, stderr=subprocess.STDOUT, shell=True, creationflags=flags)
        procs.append(("web", p))
        print(f"  web       pid {p.pid}")
    print("\nCountersign is running:")
    print("  Web app   http://localhost:3000")
    print("  API       http://127.0.0.1:8000/docs")
    print("  Broker    http://127.0.0.1:8100/health")
    print("  Logs      .logs/*.log      (Ctrl+C to stop everything)\n")
    try:
        while True:
            time.sleep(2)
            for name, p in procs:
                if p.poll() is not None:
                    print(f"  ! {name} exited with code {p.returncode}; see .logs/{name}.log")
                    procs.remove((name, p))
                    break
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping...")
        for _, p in procs:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
                else:
                    p.send_signal(signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
