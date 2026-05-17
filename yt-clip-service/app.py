"""
YT Clip Service v3 — Premier League match background clips
Source priority: YouTube highlights (via ytsearch, trim middle) → X rerank by duration → Pexels fallback
Returns direct mp4 URL on catbox.moe that Shotstack can consume as background.
"""
import os
import re
import json
import base64
import random
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- API keys / config -----------------------------------------------------
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()  # legacy, unused

# v15: residential proxy URL for yt-dlp. When set, all yt-dlp calls (search + download)
# against YouTube route through this proxy. Example values:
#   http://user:pass@gate.smartproxy.com:7000
#   socks5://user:pass@rotating-residential.oxylabs.io:7777
# This bypasses YT bot-check/SABR on VPS IPs — YouTube sees a residential IP.
YT_PROXY = os.environ.get("YT_PROXY", "").strip()
if YT_PROXY:
    # Mask credentials for log — keep host/port only
    _masked = re.sub(r"://[^@]+@", "://***@", YT_PROXY) if "@" in YT_PROXY else YT_PROXY
    print(f"[init] YT_PROXY configured: {_masked}")

# v19: User's home PC bridge (Cloudflare Tunnel) — yt-dlp runs on residential IP,
# bypasses YT football bot-check. See yt-bridge/bridge.py.
YT_BRIDGE_URL = os.environ.get("YT_BRIDGE_URL", "").strip().rstrip("/")
YT_BRIDGE_SECRET = os.environ.get("YT_BRIDGE_SECRET", "").strip()
if YT_BRIDGE_URL:
    print(f"[init] YT_BRIDGE_URL = {YT_BRIDGE_URL}")

# Cookies (legacy YT support — not needed for X path, kept for back-compat)
COOKIES_PATH = os.environ.get("YT_COOKIES_PATH", "")
_b64 = os.environ.get("YT_COOKIES_B64", "").strip()
if _b64 and not COOKIES_PATH:
    try:
        _decoded = base64.b64decode(_b64).decode("utf-8")
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "w", encoding="utf-8") as _f:
            _f.write(_decoded)
        print(f"[init] cookies written to {COOKIES_PATH} ({len(_decoded)} bytes)")
    except Exception as e:
        print(f"[init] failed to decode YT_COOKIES_B64: {e}")
        COOKIES_PATH = ""

# Broadcaster handles — FALLBACK only. These post a lot of studio/interview content
# which we must aggressively filter out. Prefer club handles (below) whenever possible.
BROADCAST_HANDLES = [
    "premierleague",
    "OptaJoe",
    "SkySportsPL",     # Sky Sports Premier League
    "SkySportsNews",   # Sky Sports News
    "SkySports",       # Sky Sports main
    "SkyFootball",     # Sky Sports Football
    "BBCSport",        # BBC Sport
    "tntsports",       # TNT Sports (ex BT Sport) — correct handle
    "ESPNFC",          # ESPN FC
]

# Official club X handles — PRIMARY source. These post actual match action clips
# (goals, chances, training drills, tactical replays) far more often than studio talk.
CLUB_X_HANDLES = {
    "Arsenal": "Arsenal",
    "Manchester City": "ManCity",
    "Manchester United": "ManUtd",
    "Liverpool": "LFC",
    "Chelsea": "ChelseaFC",
    "Tottenham": "SpursOfficial",
    "Tottenham Hotspur": "SpursOfficial",
    "Newcastle": "NUFC",
    "Newcastle United": "NUFC",
    "Aston Villa": "AVFCOfficial",
    "West Ham": "WestHam",
    "West Ham United": "WestHam",
    "Brighton": "OfficialBHAFC",
    "Brighton & Hove Albion": "OfficialBHAFC",
    "Everton": "Everton",
    "Wolves": "Wolves",
    "Wolverhampton": "Wolves",
    "Brentford": "BrentfordFC",
    "Crystal Palace": "CPFC",
    "Fulham": "FulhamFC",
    "Bournemouth": "afcbournemouth",
    "Nottingham Forest": "NFFC",
    "Leeds": "LUFC",
    "Leeds United": "LUFC",
    "Burnley": "BurnleyOfficial",
    "Leicester": "LCFC",
    "Leicester City": "LCFC",
    "Sunderland": "SunderlandAFC",
    "Ipswich": "IpswichTown",
    "Ipswich Town": "IpswichTown",
    "Southampton": "SouthamptonFC",
}

# Slow-mo / single-angle replay indicator — DEMOTE (push to end), not reject.
SLOW_MO_REGEX = re.compile(
    r"(?i)\b(slow[- ]?mo|slow motion|slo[- ]mo|another angle|different angle|\breplay\b|\bangle\b)\b"
)

# Non-match content — AGGRESSIVE REJECT.
# Interviews, press, studio, analysis, training, signings, coach reactions, etc.
FORBIDDEN_REGEX = re.compile(
    r"(?i)("
    r"\binterview\b|\bpress conference\b|\bpresser\b|"
    r"\breact(?:s|ed|ion|ions)?\b|"
    r"\banalys(?:is|es|e|ing|ed)\b|\bbreakdown\b|\bexplained\b|"
    r"\bdiscuss(?:ion|es|ing)\b|\bpodcast\b|\bstudio\b|"
    r"\bspeaks?\b|\bspeaking\b|\btells?\b|\btalks?\b|"
    r"\bsits? down\b|\bQ&?A\b|\bchats? (?:with|to)\b|"
    r"\bopinion\b|\bverdict\b|\bviews?\b|\bpunditry\b|"
    r"\bbehind the scenes\b|\bBTS\b|"
    r"\bassesses?\b|\breflects? on\b|\breviews?\b|"
    r"\bpreview\b|\broundtable\b|\bpanel\b|"
    r"\bsigns?\b|\bsigning\b|\bunveil(?:ed|ing|s)?\b|"
    r"\bwelcome(?:s|d)? to\b|\bnew signing\b|\bcontract\b|"
    r"\bdebut\b|\bfirst day\b|\barrives?\b|\bjoins?\b|"
    r"\btraining\b|\bdrill\b|\bgym\b|\bsession\b|"
    r"\bwarm[- ]?up\b|\btunnel\b|\bdressing room\b|"
    r"\bmatchday vlog\b|\bvlog\b|\bmeet the\b|"
    r"\bacademy\b|\bu18\b|\bu21\b|\bu23\b|\byouth\b|"
    r"\breserves?\b|\bfriendly\b|\bpreseason\b|\bpre[- ]?season\b|\btour\b|"
    r"\bmy (?:first|second|third|\d+th) season\b|"
    r"\bexclusive (?:with|chat)\b|\bin conversation\b|\bone[- ]on[- ]one\b|"
    # Coach/manager speech patterns (CRITICAL — these slip through club accounts)
    r"\b(?:boss|manager|head coach|gaffer)\b|"
    r"\bspeaks? on\b|\btalks? on\b|\bon (?:the )?(?:draw|win|loss|result|game|match|performance|display|defeat|victory)\b|"
    r"\bpost[- ]?match (?:reaction|thoughts|chat|interview)\b|"
    r"\bpre[- ]?match (?:reaction|thoughts|chat|interview|presser)\b|"
    # Named managers (common PL coaches)
    r"\b(?:emery|arteta|guardiola|klopp|ten hag|pochettino|postecoglou|"
    r"howe|moyes|iraola|glasner|marsch|maresca|amorim|frank|fernandez|"
    r"dyche|fonseca|slot|nuno|o'neil|cooper)\b|"
    # Other non-match slop
    r"\bfan (?:cam|zone|tv)\b|\bmatchday routine\b|\binside\b|"
    r"\bgoal of (?:the )?(?:month|season|week)\b|"  # compilation, not match
    r"\btop \d+\b|\bbest of\b|\brewind\b|\bclassic\b|"
    # v14: HARD REJECT lineup / team-news static graphics (v13 leaked these
    # as "plansza z twarzami" — graphic with faces, not football)
    r"\blineup\b|\blineups\b|\bline[- ]?up\b|"
    r"\bstarting XI\b|\bstarting eleven\b|\bstarting 11\b|"
    r"\bteam news\b|\bteam selection\b|"
    r"\bconfirmed XI\b|\bconfirmed (?:lineup|eleven|side|team)\b|"
    r"\bpredicted (?:XI|lineup|eleven)\b|"
    r"\bsquad (?:reveal|announcement|list)\b|"
    r"\bXI:\s|\bXI vs\b|\bteam vs\b|"
    r"\bsubstitutes?\b|\bbench\b|\bbenched\b|"
    r"\bno\.? \d+\b.{0,30}(?:joins|signs|named)|"  # squad number announcements
    # v16: HARD REJECT split-screen fan-cam / crowd-reaction broadcaster formats
    # (v15 leaked "górna połowa kibice + dolna akcja" — user explicit rejection)
    r"\bfan ?cam\b|\bfan ?reaction\b|\bfans react\b|"
    r"\bfans celebrate\b|\bfans go\b|"
    r"\bcrowd (?:goes|erupt|scene|react)\w*\b|"
    r"\bscenes (?:at|from|in|after)\b|\bscenes!\b|"
    r"\bvibes\b|\blimbs!?\b|"
    r"\blisten (?:to|in)\b|\bsound on\b|\bvolume up\b|"
    r"\bwatch[- ]?along\b|\breact(?:ing|ion) (?:to|video)\b|"
    r"\bsplit[- ]?screen\b|\bside[- ]?by[- ]?side\b|"
    r"\binside the stadium\b|\bstadium reaction\b|"
    r"\bpure noise\b|\bgoosebumps\b|\bpower cut\b"
    r")"
)

# ULTRA-STRICT match signal — tweet/title must contain one of these UNAMBIGUOUS
# match-footage markers. Interview tweets that mention "scored the winner" or
# "stunning goal" as PROSE are rejected.
# CASE-SENSITIVE markers: uppercase GOAL, FT:, HT: typical for goal announcements.
_STRONG_CS = re.compile(
    r"("
    r"\b\d{1,2}\s?[-–:]\s?\d{1,2}\b|"                  # score 2-1, 3:0, 0:0
    r"\bGOAL!?\b|⚽|🔴|🟢|"                             # GOAL marker / soccer emoji
    r"\bFT[:\s]|\bHT[:\s]|"                            # FT:/HT: prefix
    r"\b\d{1,3}\s?['′]\s|"                             # minute markers like 45'
    r"\bPENALTY!|\bRED CARD!?"                         # loud event markers
    r")"
)
# CASE-INSENSITIVE markers: highlights, full-time, extended highlights, etc.
_STRONG_CI = re.compile(
    r"("
    r"\bHIGHLIGHTS?\b|"
    r"\bEXTENDED\s+HIGHLIGHTS?\b|"
    r"\bFULL[- ]?TIME\b|\bHALF[- ]?TIME\b|"
    r"\bmatch report\b|\bmatch review\b|"
    r"\bhat[- ]?trick!?\b"
    r")",
    re.IGNORECASE,
)


class _StrongMatch:
    """Wrap both case-sensitive and case-insensitive strong-match regex."""

    def search(self, text):
        return _STRONG_CS.search(text) or _STRONG_CI.search(text)


STRONG_MATCH_REGEX = _StrongMatch()
REQUIRED_MATCH_REGEX = STRONG_MATCH_REGEX  # back-compat alias

TEAM_ALIASES = {
    "Manchester City": ["Manchester City", "ManCity", "Man City"],
    "Manchester United": ["Manchester United", "ManUtd", "Man Utd", "Man United"],
    "Liverpool": ["Liverpool", "LFC"],
    "Arsenal": ["Arsenal"],
    "Chelsea": ["Chelsea", "CFC"],
    "Tottenham": ["Tottenham", "Spurs", "THFC"],
    "Tottenham Hotspur": ["Tottenham", "Spurs", "THFC"],
    "Newcastle": ["Newcastle", "NUFC"],
    "Newcastle United": ["Newcastle", "NUFC"],
    "Aston Villa": ["Aston Villa", "AVFC", "Villa"],
    "West Ham": ["West Ham", "WHUFC"],
    "West Ham United": ["West Ham", "WHUFC"],
    "Brighton": ["Brighton", "BHAFC"],
    "Brighton & Hove Albion": ["Brighton", "BHAFC"],
    "Everton": ["Everton", "EFC"],
    "Wolves": ["Wolves", "WWFC"],
    "Wolverhampton": ["Wolves", "WWFC"],
    "Brentford": ["Brentford", "BFC"],
    "Crystal Palace": ["Crystal Palace", "CPFC"],
    "Fulham": ["Fulham", "FFC"],
    "Bournemouth": ["Bournemouth", "AFCB"],
    "Nottingham Forest": ["Nottingham Forest", "NFFC"],
    "Leeds": ["Leeds", "LUFC"],
    "Leeds United": ["Leeds", "LUFC"],
    "Burnley": ["Burnley"],
    "Leicester": ["Leicester", "LCFC"],
    "Leicester City": ["Leicester", "LCFC"],
    "Sunderland": ["Sunderland", "SAFC"],
    "Ipswich": ["Ipswich", "ITFC"],
    "Ipswich Town": ["Ipswich", "ITFC"],
    "Southampton": ["Southampton", "SaintsFC"],
    # La Liga
    "Real Madrid": ["Real Madrid", "Madrid", "Los Blancos"],
    "Barcelona": ["Barcelona", "Barca", "FCB"],
    "Atletico Madrid": ["Atletico Madrid", "Atletico", "Atl. Madrid"],
    "Sevilla": ["Sevilla"],
    "Valencia": ["Valencia"],
    "Villarreal": ["Villarreal"],
    "Athletic Bilbao": ["Athletic Bilbao", "Athletic Club"],
    "Real Sociedad": ["Real Sociedad"],
    "Osasuna": ["Osasuna"],
    "Girona": ["Girona"],
    # Bundesliga
    "Bayern Munich": ["Bayern Munich", "Bayern", "FCB Bayern"],
    "Borussia Dortmund": ["Borussia Dortmund", "Dortmund", "BVB"],
    "RB Leipzig": ["RB Leipzig", "Leipzig"],
    "Bayer Leverkusen": ["Bayer Leverkusen", "Leverkusen"],
    "Borussia Monchengladbach": ["Borussia Monchengladbach", "Gladbach"],
    "Eintracht Frankfurt": ["Eintracht Frankfurt", "Frankfurt"],
    "Wolfsburg": ["Wolfsburg"],
    "Freiburg": ["Freiburg"],
    # Serie A
    "Juventus": ["Juventus", "Juve"],
    "AC Milan": ["AC Milan", "Milan"],
    "Inter Milan": ["Inter Milan", "Inter", "Internazionale"],
    "Napoli": ["Napoli"],
    "AS Roma": ["AS Roma", "Roma"],
    "Lazio": ["Lazio"],
    "Atalanta": ["Atalanta"],
    "Fiorentina": ["Fiorentina"],
    # Ligue 1
    "PSG": ["PSG", "Paris Saint-Germain", "Paris SG", "Paris"],
    "Marseille": ["Marseille", "OM"],
    "Lyon": ["Lyon", "OL"],
    "Monaco": ["Monaco", "AS Monaco"],
    "Lille": ["Lille", "LOSC"],
    # Other European
    "Ajax": ["Ajax"],
    "PSV": ["PSV", "PSV Eindhoven"],
    "Feyenoord": ["Feyenoord"],
    "Porto": ["Porto", "FC Porto"],
    "Benfica": ["Benfica", "SL Benfica"],
    "Sporting CP": ["Sporting CP", "Sporting Lisbon"],
    "Celtic": ["Celtic"],
    "Rangers": ["Rangers"],
    "Shakhtar": ["Shakhtar", "Shakhtar Donetsk"],
    "Dinamo Zagreb": ["Dinamo Zagreb"],
    "Club Brugge": ["Club Brugge", "Brugge"],
}


def _team_query_part(team_name):
    aliases = TEAM_ALIASES.get(team_name, [team_name])
    parts = [f'"{a}"' if " " in a else a for a in aliases]
    return "(" + " OR ".join(parts) + ")"


def extract_teams_from_text(text):
    """Regex-based team detection using TEAM_ALIASES. Returns first two DISTINCT
    clubs in the order they appear. Case-insensitive, word-boundary aware.
    Deduplicates TEAM_ALIASES entries that share the same alias set (e.g.
    "Wolves" and "Wolverhampton" are the same club).
    Returns {"home_team": str|None, "away_team": str|None, "all_found": [..]}.
    """
    if not text:
        return {"home_team": None, "away_team": None, "all_found": []}

    # Deduplicate: group canonicals that share the same alias set. Keep the
    # first canonical as the representative.
    seen_alias_sets = {}
    canonical_to_rep = {}
    for canonical, aliases in TEAM_ALIASES.items():
        key = tuple(sorted(a.lower() for a in aliases))
        if key not in seen_alias_sets:
            seen_alias_sets[key] = canonical
        canonical_to_rep[canonical] = seen_alias_sets[key]

    # Find earliest position for each representative canonical
    rep_to_pos = {}
    for canonical, aliases in TEAM_ALIASES.items():
        rep = canonical_to_rep[canonical]
        for alias in aliases:
            pattern = r"\b" + re.escape(alias) + r"\b"
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                pos = m.start()
                if rep not in rep_to_pos or pos < rep_to_pos[rep]:
                    rep_to_pos[rep] = pos
                break  # one alias match is enough per canonical

    # Sort by position (order of mention in text)
    ordered = sorted(rep_to_pos.items(), key=lambda x: x[1])
    teams = [c for c, _ in ordered]
    return {
        "home_team": teams[0] if len(teams) > 0 else None,
        "away_team": teams[1] if len(teams) > 1 else None,
        "all_found": teams,
    }


# ---- Endpoints -------------------------------------------------------------

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "yt-clip",
        "version": 19,
        "x_configured": bool(X_BEARER_TOKEN),
        "pexels_configured": bool(PEXELS_API_KEY),
        "yt_proxy_configured": bool(YT_PROXY),
        "yt_bridge_configured": bool(YT_BRIDGE_URL),
    })


@app.post("/extract_teams")
def extract_teams_endpoint():
    """Extract home/away team from free-form narration text using TEAM_ALIASES.
    Input: {"text": "Arsenal pokonał Tottenham 3-1..."}
    Output: {"home_team": "Arsenal", "away_team": "Tottenham", "all_found": [...]}
    Order is based on first mention in the text.
    """
    data = request.get_json(force=True) or {}
    text = data.get("text", "") or ""
    result = extract_teams_from_text(text)
    result["ok"] = bool(result["home_team"] and result["away_team"])
    return jsonify(result)


@app.post("/update_bridge_url")
def update_bridge_url():
    """Hot-update YT_BRIDGE_URL without container restart.
    Called by launcher.py on user's PC when Cloudflare tunnel URL changes.
    Input: {"url": "https://new-url.trycloudflare.com"}
    """
    global YT_BRIDGE_URL
    data = request.get_json(force=True) or {}
    new_url = data.get("url", "").strip().rstrip("/")
    if not new_url:
        return jsonify({"error": "url required"}), 400
    old_url = YT_BRIDGE_URL
    YT_BRIDGE_URL = new_url
    print(f"[update_bridge_url] {old_url!r} -> {new_url!r}")
    return jsonify({"ok": True, "url": YT_BRIDGE_URL})


@app.get("/debug")
def debug():
    return jsonify({
        "X_BEARER_TOKEN_present": bool(X_BEARER_TOKEN),
        "PEXELS_API_KEY_present": bool(PEXELS_API_KEY),
        "YT_COOKIES_present": bool(COOKIES_PATH and os.path.exists(COOKIES_PATH)),
        "club_handles": list(CLUB_X_HANDLES.values()),
        "broadcast_handles": BROADCAST_HANDLES,
    })


# ---- Source search helpers -------------------------------------------------

def search_youtube_highlights(home_team, away_team, max_results=5):
    """yt-dlp ytsearch for match highlights. Returns list of dicts sorted by duration DESC
    (preferring longer = wide-angle match action, not 15s goal replays).
    """
    candidates = []
    queries = [
        f"{home_team} vs {away_team} highlights",
        f"{home_team} {away_team} full match",
        f"{home_team} Premier League highlights",
        f"{away_team} Premier League highlights",
    ]
    seen_ids = set()
    for q in queries:
        cmd = [
            "yt-dlp", "--no-warnings", "--flat-playlist",
            "--print", "%(id)s\t%(duration)s\t%(title)s",
            f"ytsearch{max_results}:{q}",
        ]
        if YT_PROXY:
            cmd += ["--proxy", YT_PROXY]
        try:
            r = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
            out = r.stdout.decode(errors="ignore")
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"[yt_search] '{q}' failed: {e}")
            continue
        for line in out.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            vid, dur_s, title = parts
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            try:
                dur = float(dur_s) if dur_s and dur_s != "NA" else 0.0
            except ValueError:
                dur = 0.0
            # Reject too short (isolated replay) and absurdly long (full live stream)
            if dur < 60 or dur > 3600:
                continue
            # HARD reject non-match content (interviews, signings, training, vlogs)
            if FORBIDDEN_REGEX.search(title):
                print(f"  [forbidden] {title[:80]}")
                continue
            # REQUIRE at least one positive match-action signal in title
            if not REQUIRED_MATCH_REGEX.search(title):
                print(f"  [no-match-signal] {title[:80]}")
                continue
            is_slow = bool(SLOW_MO_REGEX.search(title))
            candidates.append({
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "duration": dur,
                "query": q,
                "is_slow_mo": is_slow,
            })
    # Sort: slow-mo penalized to end, then longer duration first
    candidates.sort(key=lambda c: (c["is_slow_mo"], -c["duration"]))
    print(f"[yt_search] {len(candidates)} candidates")
    for c in candidates[:5]:
        print(f"  {c['duration']:.0f}s slow={c['is_slow_mo']}  {c['title'][:80]}")
    return candidates


# v14: Score pattern (X-Y) — required STRONG signal for broadcaster tweets.
# Ensures we match a specific match (e.g. "Liverpool 2-1 Chelsea"), not a generic
# "HIGHLIGHTS" compilation that could be from any team combination.
SCORE_REGEX = re.compile(r"\b\d{1,2}\s?[-–:]\s?\d{1,2}\b")


def _text_mentions_team(text, team_name):
    """Return True if `text` contains any alias of `team_name` as a whole word."""
    if not text:
        return False
    aliases = TEAM_ALIASES.get(team_name, [team_name])
    for alias in aliases:
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            return True
    return False


def _x_search_request(query, home_team=None, away_team=None):
    """Single X recent search call. Returns list of candidate dicts (unsorted).
    v14: adds `both_teams` and `has_score` booleans to each candidate so the
    caller can rank tweets mentioning BOTH teams with a score pattern highest.
    """
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
            params={
                "query": query,
                "max_results": 50,
                "tweet.fields": "created_at,public_metrics,attachments",
                "expansions": "author_id,attachments.media_keys",
                "user.fields": "username",
                "media.fields": "duration_ms,type",
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[x_search] HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
        media_map = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}
        tweets = data.get("data", [])
        out = []
        for t in tweets:
            mkeys = t.get("attachments", {}).get("media_keys", [])
            dur_ms = 0
            for mk in mkeys:
                m = media_map.get(mk, {})
                if m.get("type") == "video" and m.get("duration_ms"):
                    dur_ms = max(dur_ms, m["duration_ms"])
            dur_s = dur_ms / 1000.0
            if dur_s < 20:  # reject too-short (slow-mo single-play)
                continue
            if dur_s > 240:  # reject too-long (usually full interview/presser, 4min+)
                continue
            text = t.get("text", "")
            if FORBIDDEN_REGEX.search(text):  # HARD reject studio/interview/analysis
                continue
            if not REQUIRED_MATCH_REGEX.search(text):  # must have match-action signal
                continue
            # HARD REJECT slow-mo / replay / angle tweets (previous versions only
            # demoted them; user explicitly rejected slow-mo results multiple times)
            if SLOW_MO_REGEX.search(text):
                print(f"  [slow-mo reject] {text[:80]}")
                continue
            # v14: compute both-teams-mentioned and has-score flags for ranking
            has_home = _text_mentions_team(text, home_team) if home_team else False
            has_away = _text_mentions_team(text, away_team) if away_team else False
            both_teams = bool(has_home and has_away)
            has_score = bool(SCORE_REGEX.search(text))
            is_slow = False  # no slow-mo reaches this point
            uname = users.get(t.get("author_id"), "i")
            out.append({
                "url": f"https://x.com/{uname}/status/{t['id']}",
                "duration": dur_s,
                "likes": t.get("public_metrics", {}).get("like_count", 0),
                "is_slow_mo": is_slow,
                "both_teams": both_teams,
                "has_score": has_score,
                "text": text[:80],
                "author": uname,
            })
        return out
    except Exception as e:
        print(f"[x_search] error: {e}")
        return []


def search_x_clips(home_team, away_team):
    """PRIMARY: broadcaster accounts (@premierleague, @OptaJoe) — usually match footage.
    FALLBACK: official club accounts, but these frequently post interviews so filters
    must be aggressive.
    Both stages: FORBIDDEN_REGEX + STRONG_MATCH_REGEX (ultra-strict) + duration < 200s.
    """
    if not X_BEARER_TOKEN:
        return []

    candidates = []
    seen_ids = set()

    # BROADCAST ACCOUNTS ONLY. X API v2 (free/basic tier) enforces a small limit on
    # OR operators per query — 10 broadcasters + 6 team aliases exceeds it (HTTP 400).
    # Split broadcasters into batches of 3 and run one request per batch.
    home_q = _team_query_part(home_team)
    away_q = _team_query_part(away_team)
    batch_size = 3
    for i in range(0, len(BROADCAST_HANDLES), batch_size):
        batch = BROADCAST_HANDLES[i:i + batch_size]
        accounts = " OR ".join(f"from:{h}" for h in batch)
        q = f"({accounts}) ({home_q} OR {away_q}) has:videos -is:retweet"
        print(f"[x_search/broadcast batch={i//batch_size}] query: {q}")
        for c in _x_search_request(q, home_team=home_team, away_team=away_team):
            if c["url"] not in seen_ids:
                c["stage"] = "broadcast"
                candidates.append(c)
                seen_ids.add(c["url"])

    # v14 sort priority (descending for all booleans via negation):
    #   1. slow-mo penalized (already filtered, but defensive)
    #   2. both_teams DESC — tweets with BOTH home & away team names rank first
    #      (ensures the match is specifically about the target pair, not just one team)
    #   3. has_score DESC — tweets with score pattern X-Y rank next (specific match,
    #      not generic "HIGHLIGHTS" compilation)
    #   4. duration DESC — longer clips = wide-angle pitch action
    #   5. likes DESC — engagement as tie-breaker
    candidates.sort(key=lambda c: (
        c["is_slow_mo"],
        not c["both_teams"],
        not c["has_score"],
        -c["duration"],
        -c["likes"],
    ))
    print(f"[x_search] {len(candidates)} total candidates")
    for c in candidates[:5]:
        flags = f"both={c['both_teams']} score={c['has_score']}"
        print(f"  [{c['stage']}] @{c['author']} {c['duration']:.0f}s {flags}  {c['text']}")
    return candidates


def search_random_pl_match(max_results=10):
    """Last-resort fallback: search YouTube for ANY Premier League match highlights.
    Used only when team-specific search (YouTube + X) returns nothing.
    Broader queries, same strict filters (forbidden + required match signal).
    """
    candidates = []
    queries = [
        "premier league highlights this week",
        "premier league matchday highlights",
        "premier league extended highlights",
        "premier league goals this week",
    ]
    seen_ids = set()
    for q in queries:
        cmd = [
            "yt-dlp", "--no-warnings", "--flat-playlist",
            "--print", "%(id)s\t%(duration)s\t%(title)s",
            f"ytsearch{max_results}:{q}",
        ]
        if YT_PROXY:
            cmd += ["--proxy", YT_PROXY]
        try:
            r = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
            out = r.stdout.decode(errors="ignore")
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"[random_pl] '{q}' failed: {e}")
            continue
        for line in out.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            vid, dur_s, title = parts
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            try:
                dur = float(dur_s) if dur_s and dur_s != "NA" else 0.0
            except ValueError:
                dur = 0.0
            if dur < 60 or dur > 3600:
                continue
            if FORBIDDEN_REGEX.search(title):
                continue
            if not REQUIRED_MATCH_REGEX.search(title):
                continue
            is_slow = bool(SLOW_MO_REGEX.search(title))
            candidates.append({
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "duration": dur,
                "query": q,
                "is_slow_mo": is_slow,
            })
    candidates.sort(key=lambda c: (c["is_slow_mo"], -c["duration"]))
    print(f"[random_pl] {len(candidates)} candidates")
    for c in candidates[:5]:
        print(f"  {c['duration']:.0f}s  {c['title'][:80]}")
    return candidates


def search_pexels(query):
    if not PEXELS_API_KEY:
        return None
    try:
        page = random.randint(1, 4)
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 15, "orientation": "portrait", "page": page},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[pexels] HTTP {r.status_code}: {r.text[:200]}")
            return None
        videos = r.json().get("videos", [])
        # Fallback to page 1 if random page returned nothing
        if not videos and page > 1:
            r2 = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "per_page": 15, "orientation": "portrait", "page": 1},
                timeout=15,
            )
            videos = r2.json().get("videos", []) if r2.status_code == 200 else []
        # Collect all valid mp4 candidates (prefer >=720p, max 1920px wide)
        candidates = []
        for v in videos:
            best_url, best_w = None, 0
            for f in v.get("video_files", []):
                if f.get("file_type") != "video/mp4":
                    continue
                w = f.get("width", 0)
                if w <= 1920 and w >= 720 and w > best_w:
                    best_w = w
                    best_url = f["link"]
            if best_url:
                candidates.append(best_url)
        if not candidates:
            return None
        chosen = random.choice(candidates)
        print(f"[pexels] page={page} pool={len(candidates)} chosen={chosen[:80]}")
        return chosen
    except Exception as e:
        print(f"[pexels] error: {e}")
        return None


# ---- Download helpers ------------------------------------------------------

def download_with_ytdlp(url, out_path, download_section=None):
    """Download media via yt-dlp. If `download_section` is set (e.g. "*00:00-00:45"),
    only that slice is fetched — saves ~95% proxy bandwidth for YouTube.
    """
    cmd = [
        "yt-dlp", url,
        # tv client (YT bot-check bypass) serves only adaptive streams — need to merge
        # video+audio. "bv*+ba" = best video + best audio; "b" = best combined fallback.
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", out_path,
        "--no-warnings",
        "--retries", "3",
        "--no-playlist",
    ]
    is_youtube = "youtube.com" in url or "youtu.be" in url
    # For YouTube, bypass "sign in to confirm not a bot" using tv client (no auth required).
    if is_youtube:
        cmd += ["--extractor-args", "youtube:player_client=tv"]
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        cmd += ["--cookies", COOKIES_PATH]
    # v15: route YouTube traffic through residential proxy (if configured). Non-YT
    # sources (X/Twitter videos) download direct — no proxy (faster + no bandwidth waste).
    if YT_PROXY and is_youtube:
        cmd += ["--proxy", YT_PROXY]
    # v15: only download the requested slice (saves ~95% proxy bandwidth).
    # Requires ffmpeg (already in image) as external downloader for remuxing.
    if download_section and is_youtube:
        cmd += [
            "--download-sections", download_section,
            "--force-keyframes-at-cuts",
        ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = ""
        if hasattr(e, "stderr") and e.stderr:
            stderr = e.stderr.decode(errors="ignore")[:300]
        print(f"[ytdlp] failed for {url}: {stderr}")
        return False


def download_direct(url, out_path):
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        print(f"[direct] error: {e}")
        return False


def ffprobe_duration(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, timeout=15)
        return float(r.stdout.decode().strip())
    except Exception as e:
        print(f"[ffprobe] error: {e}")
        return 0.0


# v17: Official PL club primary colors (HEX without #). Used to generate animated
# gradient backgrounds when no usable match footage is available — guaranteed
# brand-correct, copyright-free, always works.
TEAM_COLORS = {
    "Arsenal":             ("EF0107", "FFFFFF"),  # red / white
    "Aston Villa":         ("670E36", "95BFE5"),  # claret / sky blue
    "Bournemouth":         ("DA291C", "000000"),  # red / black
    "Brentford":           ("E30613", "FBB800"),  # red / amber
    "Brighton":            ("0057B8", "FFCD00"),  # blue / yellow
    "Brighton & Hove Albion": ("0057B8", "FFCD00"),
    "Burnley":             ("6C1D45", "99D6EA"),  # claret / blue
    "Chelsea":             ("034694", "DBA111"),  # blue / gold
    "Crystal Palace":      ("1B458F", "C4122E"),  # blue / red
    "Everton":             ("003399", "FFFFFF"),  # blue / white
    "Fulham":              ("FFFFFF", "000000"),  # white / black
    "Ipswich":             ("3766A8", "DE2A1C"),  # blue / red
    "Ipswich Town":        ("3766A8", "DE2A1C"),
    "Leeds":               ("FFCD00", "1D428A"),  # yellow / blue
    "Leeds United":        ("FFCD00", "1D428A"),
    "Leicester":           ("003090", "FDBE11"),  # blue / gold
    "Leicester City":      ("003090", "FDBE11"),
    "Liverpool":           ("C8102E", "00B2A9"),  # red / teal
    "Manchester City":     ("6CABDD", "1C2C5B"),  # sky / navy
    "Manchester United":   ("DA291C", "FBE122"),  # red / yellow
    "Newcastle":           ("241F20", "FFFFFF"),  # black / white (stripes)
    "Newcastle United":    ("241F20", "FFFFFF"),
    "Nottingham Forest":   ("DD0000", "FFFFFF"),  # red / white
    "Southampton":         ("D71920", "130C0E"),  # red / black
    "Sunderland":          ("EB172B", "211E1F"),  # red / black
    "Tottenham":           ("132257", "FFFFFF"),  # navy / white
    "Tottenham Hotspur":   ("132257", "FFFFFF"),
    "West Ham":            ("7A263A", "1BB1E7"),  # claret / sky
    "West Ham United":     ("7A263A", "1BB1E7"),
    "Wolves":              ("FDB913", "231F20"),  # gold / black
    "Wolverhampton":       ("FDB913", "231F20"),
    # La Liga
    "Real Madrid":         ("FEBE10", "00529F"),  # gold / blue
    "Barcelona":           ("A50044", "004D98"),  # red / blue
    "Atletico Madrid":     ("CE3524", "272D69"),  # red / navy
    "Sevilla":             ("D4001A", "000000"),  # red / black
    "Valencia":            ("FF7F00", "000000"),  # orange / black
    "Villarreal":          ("FFE135", "003087"),  # yellow / blue
    "Athletic Bilbao":     ("EE2523", "FFFFFF"),  # red / white
    "Real Sociedad":       ("0067B1", "FFFFFF"),  # blue / white
    "Girona":              ("CD1419", "FFFFFF"),  # red / white
    # Bundesliga
    "Bayern Munich":       ("DC052D", "0066B2"),  # red / blue
    "Borussia Dortmund":   ("FDE100", "000000"),  # yellow / black
    "RB Leipzig":          ("CC0000", "001E62"),  # red / navy
    "Bayer Leverkusen":    ("E32221", "000000"),  # red / black
    "Eintracht Frankfurt": ("E1000F", "000000"),  # red / black
    "Wolfsburg":           ("65B32E", "003F2D"),  # green / dark green
    # Serie A
    "Juventus":            ("000000", "FFFFFF"),  # black / white
    "AC Milan":            ("FB090B", "000000"),  # red / black
    "Inter Milan":         ("010E80", "000000"),  # navy / black
    "Napoli":              ("12A0C7", "FFFFFF"),  # sky blue / white
    "AS Roma":             ("8E1F2F", "F6C024"),  # maroon / gold
    "Lazio":               ("87CEEB", "FFFFFF"),  # sky / white
    "Atalanta":            ("1E3E8A", "000000"),  # blue / black
    "Fiorentina":          ("4B0082", "FFFFFF"),  # purple / white
    # Ligue 1
    "PSG":                 ("004170", "DA291C"),  # navy / red
    "Marseille":           ("009AC7", "FFFFFF"),  # sky / white
    "Lyon":                ("003DA5", "FFFFFF"),  # blue / white
    "Monaco":              ("DA291C", "FFFFFF"),  # red / white
    "Lille":               ("E63329", "FFFFFF"),  # red / white
    # Other European
    "Ajax":                ("D2122E", "FFFFFF"),  # red / white
    "PSV":                 ("ED1C24", "FFFFFF"),  # red / white
    "Porto":               ("003DA5", "FFFFFF"),  # blue / white
    "Benfica":             ("CC0000", "FFFFFF"),  # red / white
    "Celtic":              ("16A951", "FFFFFF"),  # green / white
    "Rangers":             ("003DA5", "FFFFFF"),  # blue / white
}


# Names that signal "no real team detected in narration" — used as defaults in Parse Teams.
_GENERIC_TEAM_NAMES = {"football", "match", "soccer", "sport", ""}


def is_generic_team(name):
    """True when team name is a placeholder/default (no real team detected in narration)."""
    return not name or name.strip().lower() in _GENERIC_TEAM_NAMES


def get_team_color(team_name, is_home=True):
    """Return (primary_hex, secondary_hex) for a team.
    Falls back to PL brand colors — purple for home, lime for away — so unknown teams
    always produce a visually distinct gradient instead of uniform purple.
    """
    if team_name in TEAM_COLORS:
        return TEAM_COLORS[team_name]
    # Unknown/generic team: home→PL purple, away→PL lime (guaranteed contrast)
    return ("3D195B", "00FF87") if is_home else ("00FF87", "3D195B")





def generate_team_color_bg(home_team, away_team, out, duration):
    """v17: Generate animated diagonal gradient mp4 in club brand colors.

    Uses ffmpeg's `gradients` filter — smooth animated linear gradient between
    home primary and away primary colors. Subtle motion (speed=0.005) makes it
    feel alive without competing with the avatar / overlay text.
    Output: 1080x1920 portrait, 30fps, no audio, ~1-3 MB for 30s.
    """
    home_primary, _ = get_team_color(home_team, is_home=True)
    away_primary, _ = get_team_color(away_team, is_home=False)
    print(f"[bg/gradient] {home_team}=#{home_primary}  {away_team}=#{away_primary}  dur={duration}s")
    # gradients filter is in ffmpeg 5.1+; type=linear, x0/y0=top-left, x1/y1=bottom-right
    # for a diagonal blend. duration controls loop, speed controls motion rate.
    src = (
        f"gradients=size=1080x1920:c0=0x{home_primary}:c1=0x{away_primary}:"
        f"x0=0:y0=0:x1=1080:y1=1920:duration={duration}:speed=0.01:type=linear,"
        f"format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", src,
        "-t", str(duration),
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-an",
        out,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return os.path.exists(out) and os.path.getsize(out) > 0


def ffmpeg_loop_to_duration(input_path, output_path, target_duration):
    """Loop a short video clip until it fills target_duration seconds."""
    actual_dur = ffprobe_duration(input_path)
    if actual_dur <= 0:
        return False
    print(f"[loop] source={actual_dur:.1f}s → target={target_duration}s")
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", input_path,
        "-t", str(target_duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-an",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        print(f"[loop] ffmpeg failed: {result.stderr.decode(errors='ignore')[:300]}")
        return False
    return os.path.exists(output_path) and os.path.getsize(output_path) > 0


def ffmpeg_trim_portrait(raw, out, duration, offset):
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(offset),
        "-i", raw,
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-an",
        out,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)


def fetch_yt_via_bridge(home_team, away_team, raw_path, query_extra="highlights premier league",
                         min_duration=120, max_duration=900, query=None, trim_seconds=30):
    """v19: Ask user's home PC bridge to download a YT highlight clip.
    Bridge runs yt-dlp on residential IP -> bypasses VPS bot-check.
    Pass `query` to override the auto-built query string directly.
    Saves the mp4 to raw_path. Returns (ok, meta_dict).
    """
    if not YT_BRIDGE_URL:
        return False, {"error": "YT_BRIDGE_URL not configured"}
    query = query or f"{home_team} {away_team} {query_extra}"
    print(f"[bridge] -> {YT_BRIDGE_URL}/search_dl  query={query!r}  trim_seconds={trim_seconds}")
    try:
        headers = {}
        if YT_BRIDGE_SECRET:
            headers["Authorization"] = f"Bearer {YT_BRIDGE_SECRET}"
        r = requests.post(
            f"{YT_BRIDGE_URL}/search_dl",
            json={"query": query, "min_duration": min_duration, "max_duration": max_duration, "trim_seconds": trim_seconds},
            headers=headers,
            timeout=300,
            stream=True,
        )
    except requests.RequestException as e:
        print(f"[bridge] request failed: {e}")
        return False, {"error": f"bridge request: {e}"}
    if r.status_code != 200:
        body_preview = r.text[:300] if hasattr(r, "text") else "(binary)"
        print(f"[bridge] HTTP {r.status_code}: {body_preview}")
        return False, {"error": f"bridge HTTP {r.status_code}", "body": body_preview}
    meta = {
        "source_title": r.headers.get("X-Source-Title", ""),
        "source_url": r.headers.get("X-Source-Url", ""),
        "source_duration": r.headers.get("X-Source-Duration", ""),
    }
    bytes_written = 0
    with open(raw_path, "wb") as f:
        for chunk in r.iter_content(64 * 1024):
            f.write(chunk)
            bytes_written += len(chunk)
    print(f"[bridge] downloaded {bytes_written} bytes  src={meta['source_url']}")
    return os.path.getsize(raw_path) > 0, meta


def ffmpeg_trim_middle(raw, out, target_duration):
    """Trim middle `target_duration` seconds — skips intro logo + ending celebration.
    Falls back to offset=0 if the source is too short.
    """
    total = ffprobe_duration(raw)
    # Skip first 15% and last 15% of source; take target_duration from near the start of the middle region.
    if total > target_duration * 1.5:
        start_pad = max(15.0, total * 0.15)
        end_pad = max(15.0, total * 0.15)
        usable = total - start_pad - end_pad
        if usable < target_duration:
            offset = max(0.0, (total - target_duration) / 2.0)
        else:
            offset = start_pad + (usable - target_duration) / 2.0
    else:
        offset = 0.0
    print(f"[trim_middle] total={total:.1f}s offset={offset:.1f}s target={target_duration}s")
    ffmpeg_trim_portrait(raw, out, target_duration, offset)


def upload_litterbox(path, expiry="72h"):
    with open(path, "rb") as f:
        r = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": ("clip.mp4", f, "video/mp4")},
            timeout=120,
        )
    if r.status_code == 200:
        data = r.json()
        url = data.get("data", {}).get("url", "")
        if url:
            return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    return None


# ---- Main /clip endpoint ---------------------------------------------------

@app.post("/clip")
def clip():
    data = request.get_json(force=True)
    home_team = (data.get("home_team") or "").strip()
    away_team = (data.get("away_team") or "").strip()
    duration = int(data.get("duration", 30))
    offset = data.get("offset")  # optional explicit offset (used for Pexels/X)
    pexels_query = (data.get("pexels_query") or "").strip()  # context-aware query from workflow

    if not home_team or not away_team:
        return jsonify({"error": "home_team and away_team are required"}), 400

    attempts = []
    chosen_source = None
    chosen_url = None
    chosen_meta = {}

    bg_mode = (data.get("bg_mode") or "color").strip().lower()

    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.mp4")
        clip_path = os.path.join(tmp, "clip.mp4")

        # v19: yt-bridge — fetch real YT highlights through user's home PC.
        # Bypasses VPS bot-check because yt-dlp runs on residential IP.
        if bg_mode == "yt-bridge":
            ok, meta = fetch_yt_via_bridge(home_team, away_team, raw, trim_seconds=duration)
            if ok:
                try:
                    ffmpeg_trim_middle(raw, clip_path, duration)
                    chosen_source = "yt-bridge"
                    chosen_url = meta.get("source_url")
                    chosen_meta = meta
                    attempts.append({"source": "yt-bridge", "ok": True, **meta})
                    url_out = upload_litterbox(clip_path)
                    if not url_out:
                        return jsonify({"error": "litterbox upload failed"}), 502
                    return jsonify({
                        "url": url_out,
                        "source": chosen_source,
                        "source_url": chosen_url,
                        "source_meta": chosen_meta,
                        "home_team": home_team,
                        "away_team": away_team,
                        "duration": duration,
                        "attempts": attempts,
                    })
                except Exception as e:
                    err = str(e)[:300]
                    attempts.append({"source": "yt-bridge", "ok": False, "error": f"trim failed: {err}"})
                    print(f"[bg/yt-bridge] trim error: {err}")
            else:
                attempts.append({"source": "yt-bridge", "ok": False, **meta})
                print(f"[bg/yt-bridge] failed: {meta}")
            # User requirement (2026-04-27): NO fallback when yt-bridge explicitly requested.
            # Bridge failure must HALT the entire pipeline so n8n workflow stops.
            return jsonify({
                "error": "yt-bridge unavailable — workflow halted by design",
                "detail": meta,
                "attempts": attempts,
                "home_team": home_team,
                "away_team": away_team,
            }), 503

        # v20: yt-bridge-soft — YouTube-only with smart query tiering.
        # 2 teams: specific match → home team → generic
        # 1 team:  team-specific → generic
        # 0 teams: generic only
        if bg_mode in ("yt-bridge-soft", "youtube"):
            _home_real = not is_generic_team(home_team)
            _away_real = not is_generic_team(away_team)

            if _home_real and _away_real:
                _tiers = [
                    f"{home_team} vs {away_team} highlights",
                    f"{home_team} highlights football",
                    "football match highlights",
                ]
            elif _home_real:
                _tiers = [
                    f"{home_team} highlights football",
                    "football match highlights",
                ]
            elif _away_real:
                _tiers = [
                    f"{away_team} highlights football",
                    "football match highlights",
                ]
            else:
                _tiers = ["football match highlights"]

            for _i, _q in enumerate(_tiers):
                _ok, _meta = fetch_yt_via_bridge("", "", raw, query=_q, trim_seconds=duration)
                if _ok:
                    try:
                        ffmpeg_trim_middle(raw, clip_path, duration)
                        url_out = upload_litterbox(clip_path)
                        if not url_out:
                            return jsonify({"error": "litterbox upload failed"}), 502
                        return jsonify({
                            "url": url_out,
                            "source": "yt-bridge",
                            "source_url": _meta.get("source_url"),
                            "source_meta": _meta,
                            "home_team": home_team,
                            "away_team": away_team,
                            "duration": duration,
                            "attempts": attempts,
                        })
                    except Exception as _e:
                        err = str(_e)[:300]
                        attempts.append({"source": f"yt-bridge-tier-{_i}", "ok": False, "error": f"trim failed: {err}"})
                        print(f"[bg/yt-bridge-soft] tier {_i} trim error: {err}")
                else:
                    attempts.append({"source": f"yt-bridge-tier-{_i}", "ok": False, "query": _q, **_meta})
                    print(f"[bg/yt-bridge-soft] tier {_i} ({_q!r}) failed: {_meta}")

            return jsonify({
                "error": "yt-bridge-soft: all YouTube tiers failed",
                "attempts": attempts,
                "home_team": home_team,
                "away_team": away_team,
            }), 503

        # v17: PRIMARY = animated team-color gradient (no scraping, no copyright,
        # always works, TikTok-native aesthetic). User explicitly rejected match
        # footage path after split-screen / fan-cam content kept slipping through.
        if bg_mode == "color":
            try:
                if generate_team_color_bg(home_team, away_team, clip_path, duration):
                    chosen_source = "color-gradient"
                    chosen_url = None
                    h_hex, _ = get_team_color(home_team, is_home=True)
                    a_hex, _ = get_team_color(away_team, is_home=False)
                    chosen_meta = {"home_color": f"#{h_hex}", "away_color": f"#{a_hex}"}
                    attempts.append({"source": "color-gradient", "ok": True})
                    url_out = upload_litterbox(clip_path)
                    if not url_out:
                        return jsonify({"error": "litterbox upload failed"}), 502
                    return jsonify({
                        "url": url_out,
                        "source": chosen_source,
                        "source_url": chosen_url,
                        "source_meta": chosen_meta,
                        "home_team": home_team,
                        "away_team": away_team,
                        "duration": duration,
                        "attempts": attempts,
                    })
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode(errors="ignore")[:500] if e.stderr else ""
                print(f"[bg/gradient] FAILED: {err}")
                attempts.append({"source": "color-gradient", "ok": False, "error": err[:200]})
                # Fall through to legacy match-footage chain as defensive fallback

        # v19.1: dedicated pexels mode — skip YT/X chain entirely, go straight to Pexels
        # with a match-action query (not "stadium" which returns crowd/exterior shots)
        if bg_mode == "pexels":
            _pexels_queries = []
            if pexels_query:
                _pexels_queries.append(pexels_query)
            _pexels_queries += [
                "football players match pitch action premier league",
                "soccer players match pitch",
                "football pitch players game",
            ]
            for pq in _pexels_queries:
                pexels_url = search_pexels(pq)
                attempts.append({"source": "pexels", "found": bool(pexels_url), "query": pq})
                if pexels_url and download_direct(pexels_url, raw):
                    chosen_source = "pexels"
                    chosen_url = pexels_url
                    chosen_meta = {"query": pq}
                    break
            if not chosen_source:
                return jsonify({
                    "error": "pexels unavailable — no results for match-action query",
                    "attempts": attempts,
                    "home_team": home_team,
                    "away_team": away_team,
                }), 503

        # v19.1: dedicated x mode — X clips only, no fallback
        elif bg_mode == "x":
            x_candidates = search_x_clips(home_team, away_team)
            attempts.append({"source": "x", "candidates": len(x_candidates)})
            for c in x_candidates[:5]:
                if download_with_ytdlp(c["url"], raw):
                    chosen_source = "x"
                    chosen_url = c["url"]
                    chosen_meta = {"src_duration": c["duration"], "is_slow_mo": c["is_slow_mo"]}
                    break
            if not chosen_source:
                return jsonify({
                    "error": "x-clips unavailable — no downloadable clips found",
                    "attempts": attempts,
                    "home_team": home_team,
                    "away_team": away_team,
                }), 503

        # 1) YouTube highlights (long form → trim middle for pitch action)
        yt_candidates = []
        if not chosen_source and bg_mode not in ("pexels", "x"):
            yt_candidates = search_youtube_highlights(home_team, away_team)
            attempts.append({"source": "youtube", "candidates": len(yt_candidates)})
        # v15: compute middle section to download only ~(duration+15)s slice via proxy,
        # saves ~95% bandwidth vs full 10-min highlight fetch.
        window = duration + 15
        for c in yt_candidates[:3]:
            src_dur = c["duration"]
            if src_dur > window:
                sec_off = max(0, (src_dur - window) / 2.0)
            else:
                sec_off = 0
            section = f"*{int(sec_off)}-{int(sec_off + window)}"
            if download_with_ytdlp(c["url"], raw, download_section=section):
                chosen_source = "youtube"
                chosen_url = c["url"]
                chosen_meta = {"title": c["title"], "src_duration": c["duration"], "is_slow_mo": c["is_slow_mo"], "section": section}
                break

        # 2) X rerank by duration
        if not chosen_source:
            x_candidates = search_x_clips(home_team, away_team)
            attempts.append({"source": "x", "candidates": len(x_candidates)})
            for c in x_candidates[:5]:
                if download_with_ytdlp(c["url"], raw):
                    chosen_source = "x"
                    chosen_url = c["url"]
                    chosen_meta = {"src_duration": c["duration"], "is_slow_mo": c["is_slow_mo"]}
                    break

        # 3) Random Premier League match (any PL game — last-resort BEFORE Pexels)
        if not chosen_source:
            random_candidates = search_random_pl_match()
            attempts.append({"source": "random-pl", "candidates": len(random_candidates)})
            for c in random_candidates[:3]:
                src_dur = c["duration"]
                if src_dur > window:
                    sec_off = max(0, (src_dur - window) / 2.0)
                else:
                    sec_off = 0
                section = f"*{int(sec_off)}-{int(sec_off + window)}"
                if download_with_ytdlp(c["url"], raw, download_section=section):
                    chosen_source = "random-pl"
                    chosen_url = c["url"]
                    chosen_meta = {"title": c["title"], "src_duration": c["duration"], "is_slow_mo": c["is_slow_mo"], "section": section}
                    break

        # 4) Pexels team stadium
        if not chosen_source:
            pexels_query = f"{home_team} football stadium"
            pexels_url = search_pexels(pexels_query)
            attempts.append({"source": "pexels", "found": bool(pexels_url), "query": pexels_query})
            if pexels_url and download_direct(pexels_url, raw):
                chosen_source = "pexels"
                chosen_url = pexels_url

        # 5) Pexels generic
        if not chosen_source:
            pexels_url = search_pexels("football stadium")
            attempts.append({"source": "pexels-generic", "found": bool(pexels_url)})
            if pexels_url and download_direct(pexels_url, raw):
                chosen_source = "pexels-generic"
                chosen_url = pexels_url

        if not chosen_source:
            return jsonify({"error": "no source available", "attempts": attempts}), 502

        # 6) Trim — middle for YouTube highlights (pitch action in the middle),
        # explicit offset or 0 for X/Pexels
        try:
            if chosen_source in ("youtube", "random-pl"):
                ffmpeg_trim_middle(raw, clip_path, duration)
            else:
                ffmpeg_trim_portrait(raw, clip_path, duration, int(offset) if offset is not None else 0)
        except subprocess.CalledProcessError as e:
            return jsonify({
                "error": "ffmpeg failed",
                "stderr": e.stderr.decode(errors="ignore")[:1500] if e.stderr else "",
            }), 500

        # 6) Upload
        url_out = upload_litterbox(clip_path)
        if not url_out:
            return jsonify({"error": "litterbox upload failed"}), 502

        return jsonify({
            "url": url_out,
            "source": chosen_source,
            "source_url": chosen_url,
            "source_meta": chosen_meta,
            "home_team": home_team,
            "away_team": away_team,
            "duration": duration,
            "attempts": attempts,
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
