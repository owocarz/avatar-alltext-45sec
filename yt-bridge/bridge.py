"""
yt-bridge — local Flask service exposing yt-dlp via Cloudflare Tunnel.

Why: VPS yt-dlp calls hit YouTube bot-check on football content even with residential
proxies. Running yt-dlp on the user's home PC (Polish residential IP) bypasses the
bot-check because YT sees normal user traffic.

Endpoints:
  GET  /            -> health
  POST /search      -> {"query": "...", "max_results": 5} -> [{"url","title","duration"},...]
  POST /download    -> {"url": "...", "trim_seconds": 30} -> binary mp4 stream
  POST /search_dl   -> combo: search + first hit downloaded -> mp4 binary

Auth: simple shared secret in `Authorization: Bearer <BRIDGE_SECRET>` header.
"""
import os
import sys
import io
import re
import json
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, Response, abort

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

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

# ---- Config ----------------------------------------------------------------
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "7861"))
YT_DLP = os.environ.get(
    "YT_DLP_BIN",
    str(Path.home() / "AppData/Roaming/Python/Python313/Scripts/yt-dlp.exe"),
)
if not Path(YT_DLP).exists():
    YT_DLP = "yt-dlp"

# Suppress console popups when spawning yt-dlp/ffmpeg on Windows.
# Without this, every subprocess flashes a cmd window for ~0.5s — visible
# every 60s due to launcher.py heartbeat probes hitting the / health endpoint.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ffmpeg location — needed by yt-dlp for merging 1080p video+audio streams
FFMPEG_DIR = os.environ.get("FFMPEG_DIR", "").strip()
if FFMPEG_DIR and Path(FFMPEG_DIR).exists():
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
    print(f"[init] ffmpeg dir prepended to PATH: {FFMPEG_DIR}")
else:
    print(f"[init] WARN: FFMPEG_DIR not found, 1080p merge will fail")

print(f"[init] yt-dlp = {YT_DLP}")
print(f"[init] BRIDGE_SECRET set: {bool(BRIDGE_SECRET)}")
print(f"[init] listening on 127.0.0.1:{PORT}")

# Cache yt-dlp version once at boot so /health doesn't spawn yt-dlp per request.
try:
    _YT_DLP_VERSION = subprocess.check_output(
        [YT_DLP, "--version"], timeout=10, creationflags=_NO_WINDOW,
    ).decode().strip()
except Exception as _e:
    _YT_DLP_VERSION = f"error: {_e}"
print(f"[init] yt-dlp version: {_YT_DLP_VERSION}")

app = Flask(__name__)


def _check_auth():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return False
    return h[7:].strip() == BRIDGE_SECRET


@app.before_request
def _auth_gate():
    # Allow health without auth
    if request.path == "/":
        return None
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "yt-bridge", "yt_dlp_version": _YT_DLP_VERSION})


@app.post("/search")
def search():
    """Search YouTube via yt-dlp's ytsearch:N protocol — no API key needed."""
    data = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    max_results = int(data.get("max_results", 5))
    if not query:
        return jsonify({"error": "query required"}), 400

    cmd = [
        YT_DLP,
        f"ytsearch{max_results}:{query}",
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--quiet",
    ]
    print(f"[search] {query!r} max={max_results}")
    try:
        out = subprocess.check_output(cmd, timeout=60, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "yt-dlp failed", "stderr": e.output.decode(errors="ignore")[:500]}), 502
    except subprocess.TimeoutExpired:
        return jsonify({"error": "yt-dlp timeout"}), 504

    items = []
    for line in out.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append({
            "url": j.get("url") or f"https://www.youtube.com/watch?v={j.get('id','')}",
            "title": j.get("title", ""),
            "duration": j.get("duration"),
            "id": j.get("id"),
        })
    return jsonify({"query": query, "results": items})


@app.post("/download")
def download():
    """Download a YouTube URL (or yt-dlp-supported URL) and stream the mp4 back.

    Body: {"url":"https://...", "trim_seconds": 30, "trim_offset": 0}
    Returns: binary mp4 (Content-Type: video/mp4)
    """
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    trim_seconds = data.get("trim_seconds")
    trim_offset = int(data.get("trim_offset") or 0)
    if not url:
        return jsonify({"error": "url required"}), 400

    tmpdir = tempfile.mkdtemp(prefix="ytbridge_")
    out_tmpl = os.path.join(tmpdir, "out.%(ext)s")
    cmd = [
        YT_DLP,
        url,
        "-o", out_tmpl,
        "--no-warnings",
        "--quiet",
        "--no-playlist",
        # Pre-merged single mp4 — no ffmpeg required locally.
        # 1080p with audio. yt-dlp will use ffmpeg to merge video+audio.
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
    ]
    # NOTE: --download-sections requires ffmpeg locally; we don't trim here.
    # VPS yt-clip-service does the trim via its own ffmpeg after receiving the mp4.
    _ = trim_seconds  # ignored
    _ = trim_offset

    print(f"[download] {url} trim={trim_seconds}s offset={trim_offset}s")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "yt-dlp timeout"}), 504
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="ignore")[:500]
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "yt-dlp failed", "stderr": err}), 502

    # Find produced file
    files = list(Path(tmpdir).glob("out.*"))
    if not files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "no output file"}), 502
    mp4 = files[0]
    try:
        with open(mp4, "rb") as f:
            data_bytes = f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[download] OK size={len(data_bytes)} bytes")
    return Response(data_bytes, mimetype="video/mp4")


@app.post("/search_dl")
def search_dl():
    """Convenience: search → download first acceptable hit → return mp4.

    Body: {"query":"Arsenal Chelsea highlights","trim_seconds":30,"min_duration":120,"max_duration":900}
    Filters: skip results <min_duration (shorts) or >max_duration (full matches).
    """
    data = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    trim_seconds = int(data.get("trim_seconds", 30))
    trim_offset = int(data.get("trim_offset", 0))
    min_dur = int(data.get("min_duration", 120))
    max_dur = int(data.get("max_duration", 900))
    max_results = int(data.get("max_results", 8))

    # Reuse /search logic inline
    cmd_search = [
        YT_DLP, f"ytsearch{max_results}:{query}",
        "--flat-playlist", "--dump-json", "--no-warnings", "--quiet",
    ]
    print(f"[search_dl] {query!r}")
    try:
        out = subprocess.check_output(cmd_search, timeout=60, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    except Exception as e:
        return jsonify({"error": f"search failed: {e}"}), 502

    candidates = []
    for line in out.decode("utf-8", errors="ignore").splitlines():
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        dur = j.get("duration") or 0
        # Skip live streams (duration=None→0) and clips outside range.
        # Without this, live streams hang yt-dlp indefinitely → cloudflared timeout → 503.
        if not dur or dur < min_dur or dur > max_dur:
            continue
        candidates.append(j)

    if not candidates:
        return jsonify({"error": "no candidates after duration filter", "raw_count": 0}), 404

    # Try each candidate until one downloads OK
    tmpdir = tempfile.mkdtemp(prefix="ytbridge_")
    chosen = None
    last_err = None
    for c in candidates[:5]:
        url = c.get("url") or f"https://www.youtube.com/watch?v={c.get('id','')}"
        out_tmpl = os.path.join(tmpdir, "out.%(ext)s")
        # Prefer pre-merged single-file mp4 (no ffmpeg needed locally).
        cmd = [
            YT_DLP, url, "-o", out_tmpl,
            "--no-warnings", "--quiet", "--no-playlist",
            # 1080p with audio. yt-dlp will use ffmpeg to merge video+audio.
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        ]
        print(f"[search_dl] try {c.get('title','')[:60]} ({c.get('duration')}s)")
        proc = subprocess.run(cmd, capture_output=True, timeout=180, creationflags=_NO_WINDOW)
        if proc.returncode == 0:
            files = list(Path(tmpdir).glob("out.*"))
            if files:
                chosen = (c, files[0])
                break
        last_err = proc.stderr.decode(errors="ignore")[:300]
        # cleanup partial
        for p in Path(tmpdir).glob("out.*"):
            p.unlink(missing_ok=True)

    if not chosen:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "all candidates failed", "last_err": last_err}), 502

    meta, mp4 = chosen

    # v2 (2026-04-28): pre-trim on PC to reduce tunnel transfer size.
    # Quick CF tunnel can drop large streams (58MB) — sending only ~30s slice
    # cuts payload to ~5MB and removes mid-flight write timeouts on VPS side.
    total_dur = meta.get("duration") or 0
    trim_target = max(trim_seconds + 5, 35)  # a few extra sec for VPS final trim
    if total_dur > trim_target * 1.5:
        start = max(0, (int(total_dur) - trim_target) // 2)
        trimmed = Path(tmpdir) / "trim.mp4"
        cmd_trim = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(mp4),
            "-t", str(trim_target),
            "-c", "copy",
            "-movflags", "+faststart",
            str(trimmed),
        ]
        try:
            proc_trim = subprocess.run(cmd_trim, capture_output=True, timeout=60, creationflags=_NO_WINDOW)
            if proc_trim.returncode == 0 and trimmed.exists() and trimmed.stat().st_size > 0:
                print(f"[search_dl] pre-trimmed {mp4.stat().st_size} -> {trimmed.stat().st_size} bytes (start={start}s len={trim_target}s)")
                mp4 = trimmed
            else:
                print(f"[search_dl] pre-trim failed rc={proc_trim.returncode}, sending full file")
        except Exception as e:
            print(f"[search_dl] pre-trim exception: {e}, sending full file")

    try:
        with open(mp4, "rb") as f:
            data_bytes = f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[search_dl] OK size={len(data_bytes)} from {meta.get('id')}")
    resp = Response(data_bytes, mimetype="video/mp4")
    resp.headers["X-Source-Title"] = re.sub(r"[^\x20-\x7E]", "?", meta.get("title", ""))[:200]
    resp.headers["X-Source-Url"] = meta.get("url") or f"https://www.youtube.com/watch?v={meta.get('id','')}"
    resp.headers["X-Source-Duration"] = str(meta.get("duration") or "")
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
