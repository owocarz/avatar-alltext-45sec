"""
yt-bridge launcher — single entry point that:
  1. starts the local Flask bridge (bridge.py) as a subprocess
  2. starts cloudflared quick tunnel, captures the trycloudflare.com URL
  3. SSHes into the VPS, updates YT_BRIDGE_URL in /docker/yt-clip/.env
  4. recreates the yt-clip container so the new URL takes effect
  5. monitors both subprocesses and restarts on crash

Designed for Windows Startup (shell:startup) — fully unattended.
Logs to ~/yt-bridge/launcher.log.
"""
import os
import re
import sys
import io
import time
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime

import paramiko
import urllib.request
import urllib.error

# ---- Load .env ------------------------------------------------------------
def _load_dotenv(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv(Path(__file__).resolve().parent / ".env")

# ---- Config ---------------------------------------------------------------
BASE = Path(__file__).resolve().parent
PYTHON = sys.executable
BRIDGE_SCRIPT = str(BASE / "bridge.py")
CLOUDFLARED = os.environ.get("CLOUDFLARED", r"C:\cloudflared.exe")
LOG_PATH = BASE / "launcher.log"

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_USER = os.environ.get("VPS_USER", "root")
VPS_PASS = os.environ.get("VPS_PASS", "")
VPS_ENV = os.environ.get("VPS_ENV", "/docker/yt-clip/.env")
VPS_RESTART_CMD = os.environ.get("VPS_RESTART_CMD", "cd /docker/yt-clip && docker compose up -d 2>&1")

# ---- Logging --------------------------------------------------------------
_log_lock = threading.Lock()


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def push_url_to_vps(public_url):
    """Update YT_BRIDGE_URL via HTTP (no container restart) + persist to .env via SSH."""
    log(f"Pushing URL to VPS: {public_url}")
    # Step 1: hot-update in-memory URL — no container restart, no request interruption
    try:
        req = urllib.request.Request(
            "https://yt-clip.srv1278625.hstgr.cloud/update_bridge_url",
            data=f'{{"url": "{public_url}"}}'.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode(errors="ignore")
        log(f"VPS hot-update OK: {body[:200]}")
    except Exception as e:
        log(f"VPS hot-update FAILED (will still persist via SSH): {e}")
    # Step 2: persist to .env so next container restart also gets correct URL
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(VPS_HOST, username=VPS_USER, password=VPS_PASS, timeout=30)
        cmd = (
            f"if grep -q '^YT_BRIDGE_URL=' {VPS_ENV}; then "
            f"  sed -i 's|^YT_BRIDGE_URL=.*|YT_BRIDGE_URL={public_url}|' {VPS_ENV}; "
            f"else "
            f"  echo 'YT_BRIDGE_URL={public_url}' >> {VPS_ENV}; "
            f"fi"
        )
        _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        stdout.read()
        ssh.close()
        log(f"VPS .env persisted OK")
        return True
    except Exception as e:
        log(f"VPS .env persist FAILED: {e}")
        return False


def start_bridge():
    """Start bridge.py subprocess. Returns Popen."""
    log("Starting bridge.py subprocess...")
    proc = subprocess.Popen(
        [PYTHON, BRIDGE_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(BASE),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        bufsize=1,
        text=True,
    )

    def _drain():
        for line in proc.stdout:
            log(f"[bridge] {line.rstrip()}")

    threading.Thread(target=_drain, daemon=True).start()
    return proc


def start_tunnel(on_url_callback):
    """Start cloudflared quick tunnel. Calls on_url_callback(url) when URL appears.
    Returns Popen.
    """
    log("Starting cloudflared subprocess...")
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", "http://localhost:7861"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        bufsize=1,
        text=True,
    )

    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    found_url = {"url": None}

    def _drain():
        for line in proc.stdout:
            try:
                line = line.rstrip()
                log(f"[cloudflared] {line}")
                m = url_re.search(line)
                if m and m.group(0) != found_url["url"]:
                    found_url["url"] = m.group(0)
                    on_url_callback(m.group(0))
            except Exception as _e:
                log(f"[cloudflared drain error] {_e}")

    threading.Thread(target=_drain, daemon=True).start()
    return proc


PID_FILE = Path(__file__).resolve().parent / "launcher.pid"


def _acquire_lock():
    """Single-instance guard: returns True if we should proceed, False if another instance is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            result = subprocess.run(
                ["tasklist", "/fi", f"pid eq {old_pid}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if "python" in result.stdout.lower():
                return False
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    log("=" * 60)
    log("yt-bridge launcher started")
    log("=" * 60)

    bridge_proc = None
    tunnel_proc = None

    def shutdown(*_):
        log("Shutdown requested")
        for p in (bridge_proc, tunnel_proc):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    bridge_proc = start_bridge()
    time.sleep(3)  # give Flask a moment to bind port
    current_url = {"url": None}

    def _on_url(u):
        current_url["url"] = u
        push_url_to_vps(u)

    tunnel_proc = start_tunnel(_on_url)

    # Health monitor state
    health_fail_count = 0
    HEALTH_INTERVAL = 60          # seconds between health probes
    HEALTH_FAIL_THRESHOLD = 3     # consecutive fails to trigger restart
    HEALTH_TIMEOUT = 15           # per-probe timeout
    last_probe = time.time()

    # Wakeup detection: if real time jumps forward more than expected, system was suspended
    last_loop = time.time()
    WAKE_GAP_THRESHOLD = 60       # if a 10s sleep took > 60s, we were suspended

    # Monitor: restart any crashed subprocess + heartbeat tunnel
    while True:
        try:
            time.sleep(10)

            now = time.time()
            gap = now - last_loop
            last_loop = now

            # Detect wakeup from sleep — schedule immediate health probe + force tunnel restart
            if gap > WAKE_GAP_THRESHOLD:
                log(f"Detected wakeup (loop gap {gap:.0f}s) — proactively restarting tunnel")
                try:
                    tunnel_proc.terminate()
                except Exception:
                    pass
                health_fail_count = 0

            if bridge_proc.poll() is not None:
                log("Bridge crashed, restarting...")
                bridge_proc = start_bridge()
                time.sleep(2)
            if tunnel_proc.poll() is not None:
                log("Tunnel crashed, restarting...")
                current_url["url"] = None
                tunnel_proc = start_tunnel(_on_url)
                health_fail_count = 0
                continue

            # Heartbeat: probe bridge locally (bypasses Cloudflare so busy downloads don't cause false fails)
            if now - last_probe >= HEALTH_INTERVAL and current_url["url"]:
                last_probe = now
                try:
                    req = urllib.request.Request("http://localhost:7861/", headers={"User-Agent": "yt-bridge-healthz/1"})
                    with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                        if 200 <= resp.status < 400:
                            if health_fail_count > 0:
                                log(f"Heartbeat OK after {health_fail_count} fails")
                            health_fail_count = 0
                        else:
                            health_fail_count += 1
                            log(f"Heartbeat HTTP {resp.status} ({health_fail_count}/{HEALTH_FAIL_THRESHOLD})")
                except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
                    health_fail_count += 1
                    log(f"Heartbeat FAIL {type(e).__name__}: {e} ({health_fail_count}/{HEALTH_FAIL_THRESHOLD})")

                if health_fail_count >= HEALTH_FAIL_THRESHOLD:
                    log("Tunnel unhealthy after consecutive heartbeat fails — restarting tunnel proc")
                    try:
                        tunnel_proc.terminate()
                    except Exception:
                        pass
                    health_fail_count = 0
        except Exception as _loop_err:
            log(f"Monitor loop error (continuing): {_loop_err}")


if __name__ == "__main__":
    if not _acquire_lock():
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Another instance already running — exiting.")
        sys.exit(0)
    import traceback
    while True:
        try:
            main()
        except Exception:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as _f:
                    _f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] FATAL CRASH:\n{traceback.format_exc()}\n")
                    _f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Restarting in 30s...\n")
            except Exception:
                pass
            time.sleep(30)
