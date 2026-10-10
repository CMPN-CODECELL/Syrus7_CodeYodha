"""Starts the complete Countersign stack locally (100% free, no paid services).

Credential zones are enforced at process launch: each process receives only the broker keys it may hold.
  broker   : registers all key hashes (it is the "external" broker that issued them)
  executor : WRITE key only (the only process that can place/modify/cancel)
  api      : READ key + demo admin key + sandbox-only naive key; never the write key (it refuses to start if given one)
  monitor  : AUDIT (read-all) key only
  feed     : no keys
  notifier : WhatsApp alerts (Twilio). The ONLY process that holds CS_TWILIO_ACCOUNT_SID, CS_TWILIO_AUTH_TOKEN and
             CS_TWILIO_WHATSAPP_FROM (it reads them from .env itself; they are stripped from every other process). No
             broker key. Without the Twilio settings it runs in log-only mode (messages are recorded, not sent).
             The API reaches it on 127.0.0.1:8300 with the shared notifier key (.secrets/broker_keys.json "notifier").
  gateway021 (only when CS_021_UCC and CS_021_PASSWORD are set in .env): the 021 sandbox adapter. It verifies the
             read / write / admin / audit keys like the paper broker and is the ONLY process that loads the 021 UCC,
             password and access token (it reads them from .env itself; they are stripped from every other process).
  ollama   : the local model server is started (hidden) when it is installed but not running, so the AI fallback
             chain can reach it after a reboot; --no-ollama skips this.
--offline  : runs with no internet: no 021 gateway (paper broker and replay prices), and the AI is the local Ollama
             model only (CS_LLM_ORDER=ollama), with the deterministic engine when Ollama is not available.
Usage:  uv run --project backend python scripts/dev.py [--no-web] [--no-docker] [--no-021] [--no-ollama] [--offline]"""

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
        p.write_text(json.dumps({k: "cs_" + k + "_" + secrets.token_urlsafe(24) for k in ("read", "write", "admin", "naive", "audit", "notifier")}, indent=2))
    data = json.loads(p.read_text())
    if "notifier" not in data:
        # Installations created before WhatsApp alerts: add the shared API-to-notifier secret once.
        data["notifier"] = "cs_notifier_" + secrets.token_urlsafe(24)
        p.write_text(json.dumps(data, indent=2))
    return data


def has_021_credentials() -> bool:
    """True when .env (or .env.local) sets both CS_021_UCC and CS_021_PASSWORD. Values are never printed."""
    found: dict[str, bool] = {}
    for name in (".env", ".env.local"):
        f = ROOT / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in ("CS_021_UCC", "CS_021_PASSWORD"):
                    found[k.strip()] = bool(v.strip())
    return found.get("CS_021_UCC", False) and found.get("CS_021_PASSWORD", False)


def py() -> str:
    win = BACKEND / ".venv" / "Scripts" / "python.exe"
    nix = BACKEND / ".venv" / "bin" / "python"
    return str(win if win.exists() else nix if nix.exists() else sys.executable)


def ollama_exe() -> str | None:
    import shutil

    found = shutil.which("ollama")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(local) if os.name == "nt" and local.exists() else None


def ensure_ollama() -> str:
    """Starts `ollama serve` in the background when Ollama is installed but not listening on 11434. It keeps running
    after the stack stops (it unloads its model when idle). Returns a one-line status."""
    if wait_port(11434, 0.5):
        return "already running"
    exe = ollama_exe()
    if exe is None:
        return "not installed (the AI falls back to the cloud models or the deterministic engine)"
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != "nt")
    return "started" if wait_port(11434, 20) else "did not start within 20 s (see the Ollama app)"


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
    base = {kk: v for kk, v in os.environ.items() if not kk.startswith(("CS_BROKER_", "CS_021_UCC", "CS_021_PASSWORD", "CS_TWILIO_", "CS_NOTIFIER_KEY"))}
    base["PYTHONIOENCODING"] = "utf-8"
    base["PYTHONUNBUFFERED"] = "1"
    offline = "--offline" in args
    use_021 = not offline and "--no-021" not in args and has_021_credentials()
    base["CS_GATEWAY021_ENABLED"] = "true" if use_021 else "false"
    if offline:
        # Only the local model: the cloud models are never tried, so no turn waits on a network that is not there.
        base["CS_LLM_ORDER"] = "ollama"
    if offline or "--no-ollama" not in args:
        print(f"ollama: {ensure_ollama()}")

    if "--no-docker" not in args:
        print("starting postgres + valkey (docker compose)...")
        subprocess.run(["docker", "compose", "up", "-d", "postgres", "valkey"], cwd=ROOT, check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        wait_port(55432)
        wait_port(56379)
        time.sleep(1.0)
    print("bootstrapping schema + seed...")
    subprocess.run([py(), "-m", "countersign.core.bootstrap"], cwd=BACKEND, env=base, check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    plan = [
        ("broker", ["-m", "countersign.broker.app"], {"CS_BROKER_READ_KEY": k["read"], "CS_BROKER_WRITE_KEY": k["write"], "CS_BROKER_ADMIN_KEY": k["admin"], "CS_BROKER_NAIVE_KEY": k["naive"], "CS_BROKER_AUDIT_KEY": k["audit"]}, 8100),
    ]
    if use_021:
        # Started before the API so owners are linked to the 021 account on their first sign-in.
        plan.append(("gateway021", ["-m", "countersign.broker021.app"], {"CS_BROKER_READ_KEY": k["read"], "CS_BROKER_WRITE_KEY": k["write"], "CS_BROKER_ADMIN_KEY": k["admin"], "CS_BROKER_AUDIT_KEY": k["audit"]}, 8200))
    plan += [
        ("feed", ["-m", "countersign.feed.main"], {}, None),
        ("executor", ["-m", "countersign.executor.main"], {"CS_BROKER_WRITE_KEY": k["write"]}, None),
        ("api", ["-m", "countersign.api.main"], {"CS_BROKER_READ_KEY": k["read"], "CS_BROKER_ADMIN_KEY": k["admin"], "CS_BROKER_NAIVE_KEY": k["naive"], "CS_NOTIFIER_KEY": k["notifier"]}, 8000),
        ("monitor", ["-m", "countersign.monitor.main"], {"CS_BROKER_AUDIT_KEY": k["audit"]}, None),
        # Twilio values (if set in the shell) go to this process only; it also reads them from .env itself.
        ("notifier", ["-m", "countersign.notifier.main"], {"CS_NOTIFIER_KEY": k["notifier"], **{kk: v for kk, v in os.environ.items() if kk.startswith("CS_TWILIO_")}}, 8300),
    ]
    procs: list[tuple[str, subprocess.Popen]] = []
    # Windows: no console window for any child (the stack runs hidden; nothing pops up on the screen).
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    for name, argv, env_extra, port in plan:
        log = open(LOGS / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen([py(), *argv], cwd=BACKEND, env={**base, **env_extra}, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        procs.append((name, p))
        note = "; holds the 021 credentials" if name == "gateway021" else "; holds the Twilio credentials (log-only without them)" if name == "notifier" else ""
        held = [x.split("_")[2].lower() if x.startswith("CS_BROKER_") else "notifier" for x in env_extra if x.startswith("CS_BROKER_") or x == "CS_NOTIFIER_KEY"]
        print(f"  {name:9s} pid {p.pid}  (keys: {', '.join(held) or 'none'}{note})")
        if port:
            if not wait_port(port, 150 if name == "gateway021" else 90):
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
    print("  Notifier  http://127.0.0.1:8300/health   (WhatsApp alerts; log-only until CS_TWILIO_* are set in .env)")
    if use_021:
        print("  021       http://127.0.0.1:8200/health   (021 sandbox gateway)")
        owners = os.environ.get("CS_021_OWNER_EMAILS") or next((ln.split("=", 1)[1].strip() for f in (ROOT / ".env", ROOT / ".env.local") if f.exists() for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("CS_021_OWNER_EMAILS=")), "")
        if not owners:
            print("            CS_021_OWNER_EMAILS is empty: nobody is linked to the 021 account until you list sign-in emails there")
    elif offline:
        print("  Offline   paper broker + replay prices; AI = local Ollama (deterministic engine if Ollama is off)")
    else:
        print("  021       not started (set CS_021_UCC and CS_021_PASSWORD in .env to trade on the 021 sandbox)")
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
