"""
yt-dlp auto-updater — uruchamiany przez Task Scheduler co tydzien (poniedzialek 06:00).
Aktualizuje yt-dlp via pip, restartuje launcher jesli wersja sie zmienila.
Logi dolaczane do launcher.log (tag [updater]).
"""
import subprocess
import sys
import os
import time
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
LOG = BASE / "launcher.log"
PID_FILE = BASE / "launcher.pid"
PYTHON = sys.executable
PYTHONW = str(Path(sys.executable).parent / "pythonw.exe")
LAUNCHER = str(BASE / "launcher.py")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [updater] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_yt_dlp_version():
    try:
        r = subprocess.run(
            [PYTHON, "-m", "pip", "show", "yt-dlp"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
        for line in r.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except Exception as e:
        log(f"Version check error: {e}")
    return "unknown"


def restart_launcher():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            subprocess.run(
                ["taskkill", "/f", "/pid", str(old_pid)],
                capture_output=True, timeout=5, creationflags=_NO_WINDOW,
            )
            log(f"Killed old launcher PID {old_pid}")
        except Exception as e:
            log(f"Kill launcher warning (non-fatal): {e}")
    time.sleep(3)
    if not Path(PYTHONW).exists():
        log(f"pythonw not found at {PYTHONW} — skipping restart")
        return
    subprocess.Popen(
        [PYTHONW, LAUNCHER],
        cwd=str(BASE),
        creationflags=_NO_WINDOW,
    )
    log("Launcher restarted")


def main():
    log("=" * 40)
    log("yt-dlp update check started")

    before = get_yt_dlp_version()
    log(f"Current yt-dlp version: {before}")

    try:
        r = subprocess.run(
            [PYTHON, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
            creationflags=_NO_WINDOW,
        )
        if r.returncode != 0:
            log(f"pip install failed (exit {r.returncode}): {r.stderr[:300]}")
            return
    except Exception as e:
        log(f"pip install error: {e}")
        return

    after = get_yt_dlp_version()

    if before != after:
        log(f"Updated: {before} -> {after} — restarting launcher")
        restart_launcher()
    else:
        log(f"Already up to date ({after}) — no restart needed")

    log("Update check finished")


if __name__ == "__main__":
    main()
