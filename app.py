#!/usr/bin/env python3
"""HITZ — THE REAL MIXTAPE CONNECT.
Public launch version: FastAPI + SQLite, real audio file hosting/streaming.

Money honesty rule: if STRIPE_SECRET_KEY is not set in the environment,
every money action is saved as an unpaid_request labeled "payments being
connected". Nothing is ever shown as a paid balance until an Admin fulfills
a request after verified payment. No fake charges, no fake earnings.
"""
import hashlib
import hmac
import re
import html
import io
import json
import os
import secrets
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("UPLOAD_DIR") or (BASE_DIR / "data"))
UPLOAD_ROOT = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "demhits.db"
ADMIN_API_KEY = os.environ.get("HITZ_ADMIN_KEY", "")  # shared secret with the HERE owner panel; no admin login exists on this site
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PAYMENTS_CONNECTED = bool(STRIPE_SECRET_KEY)
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

MAX_TRACK_BYTES = 40 * 1024 * 1024
MAX_COVER_BYTES = 8 * 1024 * 1024
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg"}
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SPONSORED_TIERS = {3: 4900, 7: 9900, 14: 19900}  # days -> cents
SPONSORED_MAX_ACTIVE = 5
PRO_PRICE_CENTS = 999
PREMIUM_PRICE_CENTS = 699
SUB_PRICE_CENTS = 499
CASHOUT_THRESHOLD_CENTS = 2500

app = FastAPI(title="HITZ")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- DB layer
def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('artist','fan')),
        pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL,
        is_pro INTEGER NOT NULL DEFAULT 0, is_premium INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS mixtapes(
        id INTEGER PRIMARY KEY, artist_id INTEGER NOT NULL, title TEXT NOT NULL,
        cover_path TEXT, genre TEXT DEFAULT '', description TEXT DEFAULT '',
        download_mode TEXT NOT NULL DEFAULT 'free' CHECK(download_mode IN ('stream_only','free','paid')),
        price_cents INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tracks(
        id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL, title TEXT NOT NULL,
        file_path TEXT NOT NULL, track_num INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL, kind TEXT NOT NULL,
        listener_key TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS likes(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, tape_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(user_id, tape_id));
    CREATE TABLE IF NOT EXISTS follows(
        id INTEGER PRIMARY KEY, follower_id INTEGER NOT NULL, artist_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(follower_id, artist_id));
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT NOT NULL,
        tape_id INTEGER, is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payer_id INTEGER, artist_id INTEGER,
        tape_id INTEGER, amount_cents INTEGER NOT NULL, artist_cents INTEGER NOT NULL DEFAULT 0,
        platform_cents INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unpaid_request',
        meta TEXT DEFAULT '', created_at TEXT NOT NULL, fulfilled_at TEXT, expires_at TEXT);
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL, reporter_id INTEGER,
        reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS playlists(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS playlist_items(
        playlist_id INTEGER NOT NULL, tape_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(playlist_id, tape_id));
    CREATE TABLE IF NOT EXISTS premiere_notify(
        tape_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(tape_id, user_id));
    CREATE TABLE IF NOT EXISTS battles(
        id INTEGER PRIMARY KEY, tape_a INTEGER NOT NULL, tape_b INTEGER NOT NULL,
        created_by INTEGER NOT NULL, ends_at TEXT NOT NULL,
        winner_notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS battle_votes(
        battle_id INTEGER NOT NULL, user_id INTEGER NOT NULL, tape_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(battle_id, user_id));
    """)
    for stmt in (
        "ALTER TABLE users ADD COLUMN vanity TEXT",
        "ALTER TABLE users ADD COLUMN city TEXT DEFAULT ''",
        "ALTER TABLE mixtapes ADD COLUMN hosted_by TEXT DEFAULT ''",
        "ALTER TABLE mixtapes ADD COLUMN release_at TEXT",
        "ALTER TABLE mixtapes ADD COLUMN premiere_notified INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS verse_entries(
        id INTEGER PRIMARY KEY, original_tape_id INTEGER NOT NULL, entry_tape_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL);
    """)
    for stmt in (
        "ALTER TABLE mixtapes ADD COLUMN open_verse INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE mixtapes ADD COLUMN open_verse_note TEXT DEFAULT ''",
        "ALTER TABLE mixtapes ADD COLUMN kind TEXT NOT NULL DEFAULT 'tape'",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # HITZ RIPS trading cards: artist opt-in flag, pack rip-once stamp, pulls table
    for stmt in (
        "ALTER TABLE users ADD COLUMN cards_opt_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE transactions ADD COLUMN ripped_at TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS user_cards(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, artist_id INTEGER NOT NULL,
        tape_id INTEGER NOT NULL, tier TEXT NOT NULL, value_cents INTEGER NOT NULL DEFAULT 0,
        txn_id INTEGER NOT NULL, is_sample INTEGER NOT NULL DEFAULT 0,
        sold_back INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_user_cards_txn ON user_cards(txn_id);
    CREATE INDEX IF NOT EXISTS idx_user_cards_user ON user_cards(user_id);
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_vanity ON users(vanity) "
                 "WHERE vanity IS NOT NULL AND vanity<>''")
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- auth utils
def hash_password(password: str, salt: bytes = None):
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return dk.hex(), salt.hex()


def verify_password(password: str, pw_hash: str, pw_salt: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(pw_salt), 200_000)
    return hmac.compare_digest(dk.hex(), pw_hash)


def current_user(request: Request):
    token = request.cookies.get("dh_session")
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
        (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def listener_key(request: Request, user=None) -> str:
    if user:
        return f"user:{user['id']}"
    anon = request.cookies.get("dh_anon")
    if anon:
        return f"anon:{anon}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def is_admin(request: Request) -> bool:
    # Admin lives in the HERE owner panel now. This site has NO admin login;
    # the panel's server calls these APIs with the shared secret header.
    key = request.headers.get("x-admin-key", "")
    return bool(ADMIN_API_KEY) and hmac.compare_digest(key, ADMIN_API_KEY)


# ---------------------------------------------------------------- file utils
def sniff_audio(ext: str, head: bytes) -> bool:
    if ext == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if ext == ".wav":
        return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".ogg":
        return head.startswith(b"OggS")
    if ext == ".m4a":
        return len(head) >= 8 and head[4:8] == b"ftyp"
    return False


def sniff_cover(ext: str, head: bytes) -> bool:
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG")
    if ext == ".webp":
        return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
               ".ogg": "audio/ogg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}


# ---------------------------------------------------------------- stats
def tape_stats(conn, tape_id: int):
    plays = conn.execute("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='play'",
                         (tape_id,)).fetchone()["c"]
    downloads = conn.execute("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='download'",
                             (tape_id,)).fetchone()["c"]
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE tape_id=?", (tape_id,)).fetchone()["c"]
    return {"plays": plays, "downloads": downloads, "likes": likes}


def chart_rows(conn, since_iso=None, limit=50, kind=None):
    rows = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 AND (m.release_at IS NULL OR m.release_at<=?) ORDER BY m.created_at DESC",
        (now_iso(),)).fetchall()
    if kind:
        rows = [r for r in rows if (r["kind"] if "kind" in r.keys() else "tape") == kind]
    out = []
    for r in rows:
        if since_iso:
            p = conn.execute("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='play' AND created_at>=?",
                             (r["id"], since_iso)).fetchone()["c"]
            d = conn.execute("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='download' AND created_at>=?",
                             (r["id"], since_iso)).fetchone()["c"]
            l = conn.execute("SELECT COUNT(*) c FROM likes WHERE tape_id=? AND created_at>=?",
                             (r["id"], since_iso)).fetchone()["c"]
        else:
            s = tape_stats(conn, r["id"])
            p, d, l = s["plays"], s["downloads"], s["likes"]
        score = p + 2 * d + 3 * l
        out.append((score, dict(r), {"plays": p, "downloads": d, "likes": l}))
    out.sort(key=lambda x: (-x[0], -x[2]["plays"]))
    return out[:limit]


def golden_tape_id(conn):
    """The GOLDEN TAPE: the all-time #1 tape by chart score
    (plays + 2x downloads + 3x likes), only once it has real activity."""
    rows = chart_rows(conn, limit=1, kind="tape")
    return rows[0][1]["id"] if rows and rows[0][0] > 0 else None


def golden_single_id(conn):
    """The GOLDEN SINGLE: the all-time #1 single by the same chart score,
    only once it has real activity."""
    rows = chart_rows(conn, limit=1, kind="single")
    return rows[0][1]["id"] if rows and rows[0][0] > 0 else None


def sponsored_active(conn):
    return conn.execute(
        "SELECT t.*, m.title tape_title FROM transactions t LEFT JOIN mixtapes m ON m.id=t.tape_id "
        "WHERE t.kind='sponsored' AND t.status='paid' AND (t.expires_at IS NULL OR t.expires_at>?) "
        "ORDER BY t.fulfilled_at DESC", (now_iso(),)).fetchall()


def money_label():
    return "Payments connected" if PAYMENTS_CONNECTED else "Payments being connected — requests are saved, no charge is made yet"


def fmt(cents: int) -> str:
    return f"${cents/100:,.2f}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def site_base(request) -> str:
    if BASE_URL:
        return BASE_URL
    return f"{request.url.scheme}://{request.url.netloc}"


def notify(conn, user_id: int, text: str, tape_id=None):
    conn.execute("INSERT INTO notifications(user_id,text,tape_id,created_at) VALUES(?,?,?,?)",
                 (user_id, text, tape_id, now_iso()))


def is_live(tape) -> bool:
    ra = tape["release_at"] if "release_at" in tape.keys() else None
    return not ra or ra <= now_iso()


def sweep_premieres(conn):
    """Tapes whose premiere time has arrived: alert everyone who asked."""
    due = conn.execute(
        "SELECT * FROM mixtapes WHERE removed=0 AND premiere_notified=0 "
        "AND release_at IS NOT NULL AND release_at<=?", (now_iso(),)).fetchall()
    for t in due:
        subs = conn.execute("SELECT user_id FROM premiere_notify WHERE tape_id=?", (t["id"],)).fetchall()
        for s in subs:
            notify(conn, s["user_id"], f"🔥 PREMIERE LIVE: '{t['title']}' just dropped — listen now!", t["id"])
        conn.execute("UPDATE mixtapes SET premiere_notified=1 WHERE id=?", (t["id"],))
    if due:
        conn.commit()


def battle_votes(conn, battle_id: int):
    rows = conn.execute("SELECT tape_id, COUNT(*) c FROM battle_votes WHERE battle_id=? GROUP BY tape_id",
                        (battle_id,)).fetchall()
    return {r["tape_id"]: r["c"] for r in rows}


def battle_winner(conn, battle) -> int:
    """Winning tape id for a finished battle, or 0 (tie / not finished)."""
    if battle["ends_at"] > now_iso():
        return 0
    v = battle_votes(conn, battle["id"])
    a, b = v.get(battle["tape_a"], 0), v.get(battle["tape_b"], 0)
    if a == b:
        return 0
    return battle["tape_a"] if a > b else battle["tape_b"]


def sweep_battles(conn):
    due = conn.execute("SELECT * FROM battles WHERE winner_notified=0 AND ends_at<=?",
                       (now_iso(),)).fetchall()
    for b in due:
        w = battle_winner(conn, b)
        for tid in (b["tape_a"], b["tape_b"]):
            t = conn.execute("SELECT m.title, m.artist_id FROM mixtapes m WHERE m.id=?", (tid,)).fetchone()
            if not t:
                continue
            if w == tid:
                notify(conn, t["artist_id"], f"⚔️🏆 Your tape '{t['title']}' WON its Versus battle!", tid)
            elif w:
                notify(conn, t["artist_id"], f"⚔️ Your Versus battle ended — '{t['title']}' came up short this time. Run it back!", tid)
            else:
                notify(conn, t["artist_id"], f"⚔️ Your Versus battle for '{t['title']}' ended in a TIE.", tid)
        conn.execute("UPDATE battles SET winner_notified=1 WHERE id=?", (b["id"],))
    if due:
        conn.commit()


def tape_won_versus(conn, tape_id: int) -> bool:
    rows = conn.execute("SELECT * FROM battles WHERE (tape_a=? OR tape_b=?) AND ends_at<=?",
                        (tape_id, tape_id, now_iso())).fetchall()
    return any(battle_winner(conn, b) == tape_id for b in rows)


RESERVED_VANITY = {"admin", "api", "tape", "artist", "login", "signup", "logout", "dashboard",
                   "upload", "charts", "faq", "versus", "playlists", "playlist", "genre",
                   "genres", "city", "cities", "wall-of-paid", "get-paid", "notifications",
                   "embed", "cover", "stream", "download", "static", "healthz", "sitemap.xml",
                   "robots.txt", "hitz", "owner", "settings", "profile", "premiere-notify"}


def mode_badge(tape) -> str:
    if tape["download_mode"] == "stream_only":
        return "STREAM ONLY"
    if tape["download_mode"] == "paid":
        return f"DOWNLOAD {fmt(tape['price_cents'])}"
    return "FREE DOWNLOAD"


# ---------------------------------------------------------------- UI layout
CSS = """
*{box-sizing:border-box}body{margin:0;background:#000;color:#f2f2f2;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding-bottom:120px}
a{color:#e8c15a;text-decoration:none}a:hover{text-decoration:underline}
header.top{display:flex;align-items:center;gap:14px;padding:10px 16px;background:#0a0a0a;border-bottom:2px solid #c9a227;flex-wrap:wrap}
header.top img.logo{height:56px;width:auto;max-width:200px;object-fit:contain;display:block}
nav.main{display:flex;gap:14px;flex-wrap:wrap;font-weight:600}
nav.main a{color:#e8e8e8}
.gold{color:#e8c15a}.silver{color:#cfd2d6}.muted{color:#9aa0a6}
.wrap{max-width:1060px;margin:0 auto;padding:18px 14px}
.hero{text-align:center;padding:26px 10px 8px}
.hero img{max-width:560px;width:92%;height:auto;object-fit:contain}
h1,h2,h3{color:#e8c15a}
.bigfree{font-size:1.5rem;font-weight:800;color:#000;background:linear-gradient(180deg,#f4d878,#c9a227);display:inline-block;padding:8px 18px;border-radius:8px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
.card{background:#111;border:1px solid #333;border-radius:12px;padding:12px}
.card img.cover{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;background:#222}
.badge{display:inline-block;background:#c9a227;color:#000;font-weight:700;font-size:.72rem;padding:3px 8px;border-radius:20px;margin:4px 4px 0 0}
.badge.silver{background:#cfd2d6}.badge.dark{background:#222;color:#e8c15a;border:1px solid #c9a227}
.badge.gold{background:linear-gradient(180deg,#f3d27a,#c9a227);color:#000;border:1px solid #8a6d00}
.coverwrap{position:relative}
.goldentape{position:absolute;left:-8px;top:14px;width:78px;height:auto;transform:rotate(-10deg);border-radius:6px;box-shadow:0 4px 10px rgba(0,0,0,.65);z-index:3;pointer-events:none}
.gt-mini{height:22px;width:auto;vertical-align:middle;transform:rotate(-8deg);margin-right:6px;border-radius:3px}
.tapehero{position:relative;display:inline-block}
.tapehero .goldentape{width:104px;left:-14px;top:16px}
.bar{height:12px;background:#222;border-radius:6px;overflow:hidden;margin:6px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#f4d878,#c9a227)}
.countdown{font-size:1.6rem;font-weight:800;color:#e8c15a;letter-spacing:1px}
.chip{display:inline-block;background:#181818;border:1px solid #c9a227;color:#e8c15a;border-radius:20px;padding:5px 12px;margin:3px;font-weight:600}
textarea.code{width:100%;background:#0d0d0d;color:#cfd2d6;font-family:monospace;font-size:.8rem}
.btn{display:inline-block;background:linear-gradient(180deg,#f4d878,#c9a227);color:#000;font-weight:700;border:none;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:.95rem}
.btn.secondary{background:#222;color:#e8c15a;border:1px solid #c9a227}
.btn.small{padding:5px 10px;font-size:.85rem}
input,select,textarea{width:100%;padding:9px;margin:5px 0 12px;background:#181818;color:#fff;border:1px solid #444;border-radius:6px}
label{font-weight:600;font-size:.92rem}
.err{background:#3a0d0d;border:1px solid #a33;color:#ffb3b3;padding:10px 14px;border-radius:8px;margin:12px 0}
.ok{background:#0d2f16;border:1px solid #2a7;color:#b7f4cd;padding:10px 14px;border-radius:8px;margin:12px 0}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #333;padding:7px;text-align:left}
#player{position:fixed;left:0;right:0;bottom:0;background:#0a0a0a;border-top:2px solid #c9a227;padding:10px 14px;z-index:50}
#player .row{display:flex;align-items:center;gap:10px;max-width:1060px;margin:0 auto;flex-wrap:wrap}
#player button{background:#c9a227;color:#000;border:none;border-radius:50%;width:42px;height:42px;font-size:1.05rem;cursor:pointer;flex:none}
#player input[type=range]{flex:1;margin:0;min-width:110px}
.trackline{display:flex;align-items:center;gap:10px;padding:8px;border-bottom:1px solid #262626}
details{background:#111;border:1px solid #333;border-radius:10px;padding:10px 14px;margin:9px 0}
summary{cursor:pointer;font-weight:700;color:#e8c15a}
.xpromo{max-width:1060px;margin:26px auto 0;padding:14px;text-align:center;font-size:.82rem;color:#777;border-top:1px solid #1c1c1c}
.xpromo a{color:#a8893a;font-weight:600}.xpromo a:hover{color:#e8c15a}
"""

PLAYER_JS = """
let DH = {list: [], idx: 0, tapeId: null, playedTape: null};
const aud = () => document.getElementById('dh-audio');
function dhPlay(tracks, idx, tapeId){
  DH.list = tracks; DH.idx = idx; DH.playedTape = null;
  const t = tracks[idx];
  DH.tapeId = (t && t.tape) ? t.tape : tapeId;
  const a = aud(); a.src = '/stream/' + t.id;
  document.getElementById('p-title').textContent = t.title;
  document.getElementById('p-artist').textContent = t.artist || '';
  a.play();
  try{ localStorage.setItem('dh_last', JSON.stringify({tracks: tracks, idx: idx, tapeId: tapeId})); }catch(e){}
}
function dhToggle(){ const a = aud(); if(!a.src){return;} if(a.paused){a.play();} else {a.pause();} }
function dhStep(d){ if(!DH.list.length){return;} DH.idx = (DH.idx + d + DH.list.length) % DH.list.length; dhPlay(DH.list, DH.idx, DH.tapeId); }
document.addEventListener('DOMContentLoaded', () => {
  const a = aud(); if(!a) return;
  a.addEventListener('play', () => {
    document.getElementById('p-btn').textContent = '❚❚';
    if(DH.tapeId && DH.playedTape !== DH.tapeId){
      DH.playedTape = DH.tapeId;
      fetch('/play/' + DH.tapeId, {method:'POST'}).catch(()=>{});
    }
  });
  a.addEventListener('pause', () => { document.getElementById('p-btn').textContent = '▶'; });
  a.addEventListener('timeupdate', () => {
    const s = document.getElementById('p-seek');
    if(a.duration){ s.max = a.duration; s.value = a.currentTime; }
  });
  a.addEventListener('ended', () => {
    if(DH.list.length > 1){ dhStep(1); }
    const tip = document.getElementById('p-tip');
    if(tip && DH.tapeId){ tip.style.display = 'block'; tip.href = '/tape/' + DH.tapeId; }
  });
  document.getElementById('p-seek').addEventListener('input', (e) => { if(a.duration){ a.currentTime = e.target.value; } });
});
"""


def layout(request: Request, title: str, body: str, user=None, og=None) -> HTMLResponse:
    if user is None:
        user = current_user(request)
    conn = get_db()
    unread = 0
    if user:
        unread = conn.execute("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                              (user["id"],)).fetchone()["c"]
    conn.close()
    base = site_base(request)
    if og is None:
        og = {"title": "HITZ — The Real Mixtape Connect",
              "desc": "Artists upload FREE and GET PAID: 80% of downloads, 90% of tips, fan subscriptions, real charts.",
              "image": base + "/static/hitz-logo.jpg", "url": base + "/"}
    og_tags = (f'<meta property="og:type" content="website">'
               f'<meta property="og:site_name" content="HITZ">'
               f'<meta property="og:title" content="{html.escape(og["title"], quote=True)}">'
               f'<meta property="og:description" content="{html.escape(og["desc"], quote=True)}">'
               f'<meta property="og:image" content="{html.escape(og["image"], quote=True)}">'
               f'<meta property="og:url" content="{html.escape(og["url"], quote=True)}">'
               f'<meta name="twitter:card" content="summary_large_image">'
               f'<meta name="twitter:title" content="{html.escape(og["title"], quote=True)}">'
               f'<meta name="twitter:description" content="{html.escape(og["desc"], quote=True)}">'
               f'<meta name="twitter:image" content="{html.escape(og["image"], quote=True)}">'
               f'<meta name="description" content="{html.escape(og["desc"], quote=True)}">')
    if user:
        auth_links = (f'<a href="/playlists">Playlists</a><a href="/dashboard">Dashboard</a>'
                      f'<a href="/notifications">Alerts{" (" + str(unread) + ")" if unread else ""}</a>'
                      f'<a href="/logout">Log out</a>')
        if user["role"] == "artist":
            auth_links = '<a href="/upload">Upload</a>' + auth_links
    else:
        auth_links = '<a href="/login">Log in</a><a href="/signup">Sign up free</a>'
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — HITZ</title>{og_tags}
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/static/icon-192.png">
<style>{CSS}</style></head><body>
<header class="top">
<a href="/"><img class="logo" src="/static/hitz-logo.jpg" alt="HITZ — The Real Mixtape Connect"></a>
<nav class="main">
<a href="/">Discover</a><a href="/singles">\U0001F3B5 Singles</a><a href="/charts">Charts</a><a href="/radio">\U0001F4FB Radio</a><a href="/versus">⚔️ Versus</a><a href="/rips">\U0001F0CF Rips</a><a href="/get-paid">💰 Get Paid</a><a href="/wall-of-paid">Wall of Paid</a><a href="/faq">FAQ</a>
{auth_links}
</nav></header>
<div class="wrap">{body}</div>
<footer class="xpromo">from the same house &middot; the people, posts &amp; live are over on <a href="https://joincomehere.com" target="_blank" rel="noopener">HERE &rarr;</a></footer>
<div id="player"><div class="row">
<button id="p-btn" onclick="dhToggle()" aria-label="Play or pause">▶</button>
<button onclick="dhStep(-1)" aria-label="Previous track" style="font-size:.8rem">⏮</button>
<button onclick="dhStep(1)" aria-label="Next track" style="font-size:.8rem">⏭</button>
<div style="min-width:120px"><div id="p-title" style="font-weight:700">Pick a tape and press play</div>
<div id="p-artist" class="muted" style="font-size:.85rem"></div></div>
<input type="range" id="p-seek" value="0" min="0" max="100">
<a id="p-tip" class="btn small" href="#" style="display:none">Tip the artist</a>
<audio id="dh-audio" preload="none"></audio>
</div></div>
<script>{PLAYER_JS}</script>
</body></html>"""
    return HTMLResponse(page)


def tape_card(conn, tape, winner_id=None, golden_id=None, gsingle_id=None) -> str:
    s = tape_stats(conn, tape["id"])
    cover = f"/cover/{tape['id']}" if tape["cover_path"] else "/static/hitz-logo.jpg"
    winner = '<span class="badge">🏆 CHART WINNER</span>' if winner_id == tape["id"] else ""
    sponsored = conn.execute(
        "SELECT 1 FROM transactions WHERE tape_id=? AND kind='sponsored' AND status='paid' "
        "AND (expires_at IS NULL OR expires_at>?)", (tape["id"], now_iso())).fetchone()
    sp = '<span class="badge silver">SPONSORED</span>' if sponsored else ""
    live = is_live(tape)
    prem = "" if live else f'<span class="badge">\U0001F552 PREMIERE — drops {tape["release_at"][:10]}</span>'
    hosted = f'<span class="badge silver">\U0001F3A7 HOSTED BY {html.escape(tape["hosted_by"].upper())}</span>' if tape.get("hosted_by") else ""
    vspill = '<span class="badge gold">\u2694\uFE0F VERSUS WINNER</span>' if tape_won_versus(conn, tape["id"]) else ""
    ovpill = '<span class="badge">\U0001F3A4 OPEN VERSE</span>' if tape.get("open_verse") else ""
    artist_href = f"/@{tape['artist_vanity']}" if tape.get("artist_vanity") else f"/artist/{tape['artist_id']}"
    genre_html = (f"<a href='/genre/{slugify(tape['genre'])}'>{html.escape(tape['genre'])}</a>"
                  if tape["genre"] else "")
    stats_line = (f"\u25B6 {s['plays']} plays \u00B7 \u2B07 {s['downloads']} downloads \u00B7 \u2665 {s['likes']} likes"
                  if live else f"\U0001F525 Premiere \u2014 drops {tape['release_at'][:10]}. Be here when it lands.")
    is_single = tape.get("kind") == "single"
    singlepill = '<span class="badge gold">\U0001F3B5 SINGLE</span>' if is_single else ""
    golden = ('<img class="goldentape" src="/static/golden-tape.jpg" alt="GOLDEN TAPE" '
              'title="GOLDEN TAPE - the most-played tape on HITZ">') if golden_id == tape["id"] else ""
    goldpill = '<span class="badge gold">\U0001F947 GOLDEN TAPE</span>' if golden_id == tape["id"] else ""
    if is_single and gsingle_id == tape["id"]:
        golden = ('<img class="goldentape" src="/static/golden-tape.jpg" alt="GOLDEN SINGLE" '
                  'title="GOLDEN SINGLE - the most-played single on HITZ">')
        goldpill = '<span class="badge gold">\U0001F947 GOLDEN SINGLE</span>'
    return f"""<div class="card">
<div class="coverwrap"><a href="/tape/{tape['id']}"><img class="cover" src="{cover}" alt="cover"></a>{golden}</div>
<div style="margin-top:8px"><a href="/tape/{tape['id']}" style="font-weight:800;color:#fff">{html.escape(tape['title'])}</a></div>
<div class="muted">by <a href="{artist_href}">{html.escape(tape['artist_name'])}</a> · {genre_html}</div>
<div><span class="badge dark">{mode_badge(tape)}</span>{singlepill}{winner}{goldpill}{vspill}{ovpill}{prem}{hosted}{sp}</div>
<div class="muted" style="margin-top:6px" data-plays="{s['plays']}" data-downloads="{s['downloads']}" data-likes="{s['likes']}">
▶ {s['plays']} plays · ⬇ {s['downloads']} downloads · ♥ {s['likes']} likes</div>
<button class="btn small" style="margin-top:8px" onclick="dhShare('{html.escape(tape['artist_name'], quote=True)}','{html.escape(tape['title'], quote=True)}')">Share</button>
</div>"""


SHARE_JS = """
<script>
function dhShare(artist, title){
  const msg = '🔥 ' + artist + ' — "' + title + '" on HITZ, The Real Mixtape Connect. Artists keep 80% of sales, 90% of tips. Free to upload, free to listen.';
  if(navigator.clipboard){ navigator.clipboard.writeText(msg).then(()=>alert('Promo message copied — paste it anywhere.')); }
  else { prompt('Copy this promo message:', msg); }
}
</script>"""


# ---------------------------------------------------------------- public pages
@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "hitz", "payments_connected": PAYMENTS_CONNECTED}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "HITZ — The Real Mixtape Connect", "short_name": "HITZ",
        "start_url": "/", "display": "standalone", "background_color": "#000000",
        "theme_color": "#c9a227",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/")
def home(request: Request):
    conn = get_db()
    user = current_user(request)
    latest = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 AND m.kind='tape' ORDER BY m.created_at DESC LIMIT 12").fetchall()
    singles_latest = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 AND m.kind='single' ORDER BY m.created_at DESC LIMIT 6").fetchall()
    trending = chart_rows(conn, limit=6)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=1)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    gsingle_id = golden_single_id(conn)
    sponsored = sponsored_active(conn)
    sp_ids = [r["tape_id"] for r in sponsored]
    cards = "".join(tape_card(conn, dict(t), winner_id, golden_id, gsingle_id) for t in latest)
    single_cards = "".join(tape_card(conn, dict(t), winner_id, golden_id, gsingle_id) for t in singles_latest)
    singles_section = (f"<h2 style='margin-top:28px'>\U0001F3B5 Fresh Singles</h2><div class='grid'>{single_cards}</div>"
                       f"<p><a class='chip' href='/singles'>All singles \u2192</a></p>") if singles_latest else ""
    trend_cards = "".join(tape_card(conn, t, winner_id, golden_id, gsingle_id) for _, t, _ in trending if t["id"] not in [x["id"] for x in latest])
    sp_cards = ""
    if sp_ids:
        rows = conn.execute(
            "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
            f"WHERE m.id IN ({','.join('?' * len(sp_ids))})", sp_ids).fetchall()
        sp_cards = ("<h2>Sponsored</h2><div class='grid'>" +
                    "".join(tape_card(conn, dict(r), winner_id, golden_id, gsingle_id) for r in rows) + "</div>")
    conn.close()
    if not latest and not singles_latest:
        empty = """<div class="card" style="text-align:center;padding:28px">
<h3>Be The First</h3><p class="muted">No tapes yet. The first artist to drop sets the tone.</p>
<a class="btn" href="/signup">Create a free Artist account</a></div>"""
        cards = empty
    body = f"""
<div class="hero"><img src="/static/hitz-logo.jpg" alt="HITZ logo">
<h1>HITZ — <span class="silver">THE REAL MIXTAPE CONNECT</span></h1>
<div class="bigfree">FREE TO UPLOAD</div>
<p style="font-size:1.1rem">Artists post mixtapes and singles free, fans stream free — and artists actually <b class="gold">get paid</b>:
80% of sales, 90% of tips, 80% of fan subs.</p>
<p><a class="btn" href="/radio">\U0001F4FB Play HITZ Radio</a> <a class="btn secondary" href="/get-paid">💰 See how you get paid</a> <a class="btn secondary" href="/signup">Create Free Artist Account</a></p>
</div>
{sp_cards}
<h2>Latest Drops</h2><div class="grid">{cards}</div>
{singles_section}
<h2 style="margin-top:28px">Trending</h2><div class="grid">{trend_cards or '<p class="muted">Nothing trending yet.</p>'}</div>
<h2 style="margin-top:28px">Browse</h2><p><a class="chip" href="/genres">\U0001F3B6 Genres</a><a class="chip" href="/cities">\U0001F4CD Cities</a><a class="chip" href="/versus">\u2694\uFE0F Versus Battles</a><a class="chip" href="/wall-of-paid">\U0001F4B0 Wall of Paid</a></p>
{SHARE_JS}"""
    return layout(request, "Discover", body, user)


@app.get("/charts")
def charts(request: Request):
    conn = get_db()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=20)
    alltime = chart_rows(conn, limit=20)
    top_singles = chart_rows(conn, limit=20, kind="single")
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None

    golden_id = golden_tape_id(conn)

    def rows(data, mark_golden=False, noun="Tape"):
        if not data or all(s == 0 for s, _, _ in data):
            return "<p class='muted'>No chart activity yet — be the first.</p>"
        trs = ""
        for i, (score, t, s) in enumerate(data, 1):
            gt = ("<img class='gt-mini' src='/static/golden-tape.jpg' alt='GOLDEN TAPE' "
                  "title='GOLDEN TAPE'>") if (mark_golden and t['id'] == golden_id) else ""
            trs += (f"<tr><td>{i}</td><td>{gt}<a href='/tape/{t['id']}'>{html.escape(t['title'])}</a>"
                    f"{' 🏆' if t['id'] == winner_id else ''}</td>"
                    f"<td><a href='/artist/{t['artist_id']}'>{html.escape(t['artist_name'])}</a></td>"
                    f"<td>{mode_badge(t)}</td><td>{s['plays']}</td><td>{s['downloads']}</td><td>{s['likes']}</td></tr>")
        return (f"<table><tr><th>#</th><th>{noun}</th><th>Artist</th><th>Mode</th>"
                "<th>Plays</th><th>Downloads</th><th>Likes</th></tr>" + trs + "</table>")

    body = f"<h1>Charts</h1><h2>Top This Week</h2>{rows(weekly)}<h2 style='margin-top:30px'>Top All-Time</h2>{rows(alltime, mark_golden=True)}<h2 style='margin-top:30px'>\U0001F3B5 Top Singles</h2>{rows(top_singles, noun='Single')}{SHARE_JS}"
    conn.close()
    return layout(request, "Charts", body)


@app.get("/singles")
def singles_page(request: Request):
    conn = get_db()
    user = current_user(request)
    latest = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 AND m.kind='single' ORDER BY m.created_at DESC LIMIT 24").fetchall()
    top = chart_rows(conn, limit=6, kind="single")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=1)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    gsingle_id = golden_single_id(conn)
    latest_ids = [t["id"] for t in latest]
    cards = "".join(tape_card(conn, dict(t), winner_id, golden_id, gsingle_id) for t in latest)
    top_cards = "".join(tape_card(conn, t, winner_id, golden_id, gsingle_id) for _, t, _ in top if t["id"] not in latest_ids)
    conn.close()
    if not latest:
        body = """<h1>\U0001F3B5 Singles</h1>
<div class="card" style="text-align:center;padding:28px">
<h3>Be The First</h3><p class="muted">No singles yet. Got one song ready? Drop it — no mixtape needed.</p>
<a class="btn" href="/signup">Create a free Artist account</a></div>""" + SHARE_JS
        return layout(request, "Singles", body, user)
    top_section = f"<h2 style='margin-top:28px'>Top Singles</h2><div class='grid'>{top_cards}</div>" if top_cards else ""
    body = f"""<h1>\U0001F3B5 Singles</h1>
<p class="muted">One song, all yours. Singles stream free, chart on their own board, and earn the same way tapes do — downloads, tips and subs.</p>
<h2>Fresh Singles</h2><div class="grid">{cards}</div>
{top_section}
{SHARE_JS}"""
    return layout(request, "Singles", body, user)


@app.get("/get-paid")
def get_paid(request: Request):
    conn = get_db()
    active = len(sponsored_active(conn))
    conn.close()
    spots_left = max(0, SPONSORED_MAX_ACTIVE - active)
    body = f"""
<h1>💰 Get Paid on HITZ</h1>
<div class="bigfree">FREE TO UPLOAD — FREE TO EARN</div>
<p>No card. No upload fee. No follower minimum. You only ever pay if you <i>want</i> optional promo or Pro perks.
Pro, Sponsored and Premium are <b>always optional</b> — never required to upload or earn.</p>
<p class="muted">{money_label()}. Cash-out threshold: {fmt(CASHOUT_THRESHOLD_CENTS)}. Artists are paid only from money actually collected — never fake balances.</p>
<div class="grid">
<div class="card"><h3>Paid Downloads — keep 80%</h3><p>Set your price. You keep 80% of every download sale. <b>Pro artists keep 85%.</b></p></div>
<div class="card"><h3>Tips — keep 90%</h3><p>Fans tip $1 / $5 / $10 or any amount on your tape. You keep 90%.</p></div>
<div class="card"><h3>Fan Subscriptions — keep 80%</h3><p>Fans subscribe to you for {fmt(SUB_PRICE_CENTS)}/mo. You keep 80%, every month they stay.</p></div>
<div class="card"><h3>Stream Share</h3><p>A share of the Fan Premium + ads pool, split by your real plays. Free plays build the audience; the pool is funded by Premium and ads.</p></div>
<div class="card"><h3>Sponsored Spots — {spots_left} of {SPONSORED_MAX_ACTIVE} open</h3><p>Homepage placement: $49 / 3 days · $99 / 7 days · $199 / 14 days. Optional promo, bought by artists and labels.</p></div>
<div class="card"><h3><a href="/wall-of-paid">Wall of Paid</a></h3><p>Every real artist payout, posted in public. Proof pays better than promises — <a href="/wall-of-paid">see the wall</a>.</p></div>
<div class="card"><h3>Built for artist paydays</h3><p>Promo alone doesn't pay rent. On HITZ every tape can turn plays into money — downloads, tips, subs and stream share, all tracked in your earnings dashboard.</p></div>
</div>
<h2>Earnings Calculator</h2>
<div class="card">
<label>Paid downloads sold <input type="number" id="c-dl" value="100" min="0"></label>
<label>Price per download ($) <input type="number" id="c-price" value="10" min="0" step="0.5"></label>
<label>Total tips received ($) <input type="number" id="c-tips" value="100" min="0"></label>
<label>Fan subscribers <input type="number" id="c-subs" value="20" min="0"></label>
<label><span style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="c-pro" style="width:auto;margin:0"> I'm on Artist Pro (85% of sales)</span></label>
<p style="font-size:1.2rem">Estimated artist earnings: <b class="gold" id="c-out">$0.00</b> <span class="muted">/ month for subs</span></p>
<p class="muted">Example estimate — not a guarantee. Earnings depend on real fans actually paying. Stripe fees come out before the split.</p>
</div>
<script>
function calc(){{
  const dl=+document.getElementById('c-dl').value||0, pr=+document.getElementById('c-price').value||0,
        tips=+document.getElementById('c-tips').value||0, subs=+document.getElementById('c-subs').value||0,
        pro=document.getElementById('c-pro').checked;
  const total = dl*pr*(pro?0.85:0.80) + tips*0.90 + subs*{SUB_PRICE_CENTS/100}*0.80;
  document.getElementById('c-out').textContent = '$' + total.toFixed(2);
}}
document.querySelectorAll('#c-dl,#c-price,#c-tips,#c-subs,#c-pro').forEach(e=>e.addEventListener('input',calc)); calc();
</script>
<h2>Download Modes — you control your tape</h2>
<div class="grid">
<div class="card"><h3>STREAM ONLY</h3><p>Fans listen on the site only. Downloads are off for everyone — even Premium. Your setting always wins.</p></div>
<div class="card"><h3>FREE DOWNLOAD</h3><p>Any fan with a free account can download. Downloading follows you automatically, so free promo builds your fan list.</p></div>
<div class="card"><h3>PAID DOWNLOAD</h3><p>Fans stream free, but the download costs your price (fans can pay more to support you). You keep 80%, or 85% on Pro.</p></div>
</div>
<p style="margin-top:18px"><a class="btn" href="/signup">Create Free Artist Account</a></p>
"""
    return layout(request, "Get Paid", body)


FAQ_ITEMS = [
    ("Is it free to upload?", "YES. Creating an Artist account and uploading mixtapes is completely free — no card, no upload fee. You can post, get a page, and earn from day one."),
    ("Do I need Pro or Sponsored to upload or earn?", "No. Artist Pro ($9.99/mo), Sponsored placement ($49/$99/$199) and Fan Premium ($6.99/mo) are optional extras. Pro adds a verified badge and a better 85/15 sales split; Sponsored buys homepage promo. Neither is ever required to upload or earn."),
    ("How do I make money as an artist?", "Four ways: paid downloads (you keep 80%, or 85% on Pro), tips (you keep 90%), fan subscriptions (you keep 80%), and stream share from the Fan Premium + ads pool split by your real plays."),
    ("How and when do I get paid?", "Earnings build in your Artist Earnings Dashboard, split into Paid and Pending. Cash-out starts at $25. You are paid only from money actually collected from fans — HITZ never pays out of its own pocket and never shows fake balances."),
    ("What does HITZ keep?", "20% of paid download sales (15% if you are Pro), 10% of tips, and 20% of fan subscriptions. Sponsored, Pro and Premium fees are platform revenue. Stripe fees come out before any split."),
    ("Do I keep the rights to my music?", "Yes — you keep 100% of your rights. At upload you confirm the music is yours or you have permission to post it. That protects you and the platform."),
    ("Can I control downloads of my tape?", "Yes. Every tape has a download mode you pick at upload and can change anytime: Stream Only (no downloads for anyone), Free Download (free Fan account), or Paid Download (stream free, download costs your price)."),
    ("What counts as a play?", "A play counts when a listener actually starts playing your tape. The same listener replaying the same tape on the same day does not inflate your count — plays are deduped per listener per day."),
    ("How do free downloads help me if they're free?", "A free download requires a free Fan account and automatically follows you, so every free download grows your fan list. Those followers get alerted on your next drop — and buy the next tape."),
    ("What is Fan Premium?", "An optional $6.99/mo fan plan for unlimited downloads of tapes artists set as free or paid-for-download (paid tapes still cost their price unless purchased). Premium is never required to listen."),
    ("What if someone posts music that isn't theirs?", "Every tape has a Report / DMCA button. Reports go to the Admin moderation view, tapes can be removed, and repeat infringers can be banned. Only upload music you own or have permission for."),
]


@app.get("/faq")
def faq(request: Request):
    items = "".join(f"<details><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>"
                    for q, a in FAQ_ITEMS)
    body = f"""<h1>FAQ</h1>
<div class="bigfree">FREE TO UPLOAD</div>
<p class="muted">Straight answers, no fine print games. <a href="/get-paid">See the full Get Paid page →</a></p>
{items}"""
    return layout(request, "FAQ", body)



# ---------------------------------------------------------------- growth: wall, browse, embed, vanity, seo
@app.get("/wall-of-paid")
def wall_of_paid(request: Request):
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, a.display_name artist_name, a.vanity artist_vanity, m.title tape_title "
        "FROM transactions t JOIN users a ON a.id=t.artist_id LEFT JOIN mixtapes m ON m.id=t.tape_id "
        "WHERE t.status='paid' AND t.artist_cents>0 AND t.kind IN ('tip','paid_download','subscription','card_pool') "
        "ORDER BY t.fulfilled_at DESC LIMIT 100").fetchall()
    total = sum(r["artist_cents"] for r in rows)
    conn.close()
    labels = {"tip": "Tip", "paid_download": "Download sale", "subscription": "Fan subscription",
              "card_pool": "🃏 Trading card pack pool"}
    lines = ""
    for r in rows:
        href = f"/@{r['artist_vanity']}" if r["artist_vanity"] else f"/artist/{r['artist_id']}"
        when = (r["fulfilled_at"] or r["created_at"])[:10]
        tape = f" — '{html.escape(r['tape_title'])}'" if r["tape_title"] else ""
        lines += (f"<div class='card' style='margin:8px 0'><b class='gold'>{fmt(r['artist_cents'])}</b> "
                  f"paid to <a href='{href}'>{html.escape(r['artist_name'])}</a> "
                  f"<span class='muted'>{labels.get(r['kind'], r['kind'])}{tape} · {when}</span></div>")
    body = f"""<h1>\U0001F4B0 Wall of Paid</h1>
<p>Every artist payout on HITZ, in public. Money only lands here after it was <b>actually collected and paid out</b> — no fake balances, ever.</p>
<p style="font-size:1.3rem">Total paid to artists: <b class="gold">{fmt(total)}</b></p>
{lines or '<div class="card" style="text-align:center;padding:24px"><h3>Be the first name on this wall</h3><p class="muted">No payouts yet. The first artist to cash out makes history here.</p><a class="btn" href="/signup">Create a free Artist account</a></div>'}
<p class="muted">How artists earn: <a href="/get-paid">Get Paid page</a>.</p>"""
    return layout(request, "Wall of Paid", body)


@app.get("/genres")
def genres_index(request: Request):
    conn = get_db()
    rows = conn.execute("SELECT genre, COUNT(*) c FROM mixtapes WHERE removed=0 AND genre<>'' GROUP BY genre ORDER BY c DESC").fetchall()
    conn.close()
    chips = "".join(f"<a class='chip' href='/genre/{slugify(r['genre'])}'>{html.escape(r['genre'])} ({r['c']})</a>" for r in rows)
    return layout(request, "Genres", f"<h1>Genres</h1><p>{chips or '<span class=muted>No genres yet.</span>'}</p>")


@app.get("/genre/{slug}")
def genre_page(request: Request, slug: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city "
        "FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.removed=0 ORDER BY m.created_at DESC").fetchall()
    match = [dict(r) for r in rows if slugify(r["genre"] or "") == slug]
    if not match:
        conn.close()
        return PlainTextResponse("Genre not found", status_code=404)
    weekly = chart_rows(conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), limit=50)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    cards = "".join(tape_card(conn, t, winner_id, golden_id) for t in match)
    name = match[0]["genre"]
    conn.close()
    body = f"<h1>{html.escape(name)}</h1><p class='muted'>{len(match)} tape(s) · <a href='/genres'>All genres</a></p><div class='grid'>{cards}</div>{{SHARE}}"
    return layout(request, name, body.replace("{SHARE}", SHARE_JS))


@app.get("/cities")
def cities_index(request: Request):
    conn = get_db()
    rows = conn.execute("SELECT city, COUNT(*) c FROM users WHERE role='artist' AND city<>'' GROUP BY city ORDER BY c DESC").fetchall()
    conn.close()
    chips = "".join(f"<a class='chip' href='/city/{slugify(r['city'])}'>{html.escape(r['city'])} ({r['c']} artists)</a>" for r in rows)
    return layout(request, "Cities", f"<h1>Cities</h1><p>{chips or '<span class=muted>No hometowns set yet.</span>'}</p>")


@app.get("/city/{slug}")
def city_page(request: Request, slug: str):
    conn = get_db()
    artists = [dict(r) for r in conn.execute("SELECT * FROM users WHERE role='artist'").fetchall()
               if slugify(r["city"] or "") == slug]
    if not artists:
        conn.close()
        return PlainTextResponse("City not found", status_code=404)
    ids = [a["id"] for a in artists]
    rows = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city "
        f"FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.removed=0 AND m.artist_id IN ({','.join('?' * len(ids))}) "
        "ORDER BY m.created_at DESC", ids).fetchall()
    weekly = chart_rows(conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), limit=50)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    cards = "".join(tape_card(conn, dict(r), winner_id, golden_id) for r in rows)
    chips = "".join(
        f"<a class='chip' href='/{('@' + a['vanity']) if a['vanity'] else 'artist/' + str(a['id'])}'>{html.escape(a['display_name'])}</a>"
        for a in artists)
    name = artists[0]["city"]
    conn.close()
    body = f"<h1>\U0001F4CD {html.escape(name)}</h1><h3>Artists</h3><p>{chips}</p><h3>Tapes</h3><div class='grid'>{cards or '<p class=muted>No tapes yet.</p>'}</div>{{SHARE}}"
    return layout(request, name, body.replace("{SHARE}", SHARE_JS))


@app.get("/embed/{tape_id}")
def embed_player(request: Request, tape_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.id=?",
        (tape_id,)).fetchone()
    if not row or row["removed"]:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    tape = dict(row)
    tracks = conn.execute("SELECT * FROM tracks WHERE tape_id=? ORDER BY track_num", (tape_id,)).fetchall()
    conn.close()
    base = site_base(request)
    live = is_live(tape)
    if live:
        players = "".join(
            f"<div style='margin:8px 0'><div style='font-size:.9rem;margin-bottom:3px'>{i+1}. {html.escape(t['title'])}</div>"
            f"<audio controls preload='none' src='/stream/{t['id']}' style='width:100%'></audio></div>"
            for i, t in enumerate(tracks))
        inner = players + f"<p style='margin:10px 0 0'><a href='{base}/tape/{tape_id}' target='_blank' style='color:#e8c15a'>Listen on HITZ \u2192</a></p>"
    else:
        inner = f"<p style='font-size:1.05rem'>\U0001F552 PREMIERE — drops {tape['release_at'][:10]}</p><p><a href='{base}/tape/{tape_id}' target='_blank' style='color:#e8c15a'>Get notified on HITZ \u2192</a></p>"
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(tape['title'])} — HITZ</title></head>
<body style="margin:0;background:#0a0a0a;color:#f2f2f2;font-family:system-ui,sans-serif;padding:12px">
<div style="display:flex;gap:12px;align-items:center">
<img src="{base}/cover/{tape_id}" alt="cover" style="width:84px;height:84px;object-fit:cover;border-radius:8px">
<div><div style="font-weight:800;font-size:1.05rem">{html.escape(tape['title'])}</div>
<div style="color:#9aa0a6">{html.escape(tape['artist_name'])}{' · \U0001F3A7 Hosted by ' + html.escape(tape['hosted_by']) if tape.get('hosted_by') else ''}</div>
<div style="color:#e8c15a;font-weight:700;font-size:.85rem">HITZ — THE REAL MIXTAPE CONNECT</div></div></div>
{inner}
<p style="margin:12px 0 0;font-size:.9rem"><a href="{base}/signup" target="_blank" style="color:#e8c15a;font-weight:700">Drop your tape on HITZ — FREE \u2192</a></p>
<script>
let counted = false;
document.querySelectorAll('audio').forEach(a => a.addEventListener('play', () => {{
  if(!counted){{ counted = true; fetch('/play/__TAPE__', {{method:'POST'}}).catch(()=>{{}}); }}
}}));
</script></body></html>""".replace("__TAPE__", str(tape_id))
    return HTMLResponse(page, headers={"Content-Security-Policy": "frame-ancestors *"})


@app.get("/@{vanity}")
def vanity_page(request: Request, vanity: str):
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE vanity=? AND role='artist'", (vanity.lower(),)).fetchone()
    conn.close()
    if not row:
        return PlainTextResponse("Artist not found", status_code=404)
    return artist_page(request, row["id"])


@app.get("/robots.txt")
def robots(request: Request):
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {site_base(request)}/sitemap.xml\n",
                             media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap(request: Request):
    base = site_base(request)
    conn = get_db()
    tapes = conn.execute("SELECT id, release_at FROM mixtapes WHERE removed=0").fetchall()
    artists = conn.execute("SELECT id, vanity FROM users WHERE role='artist'").fetchall()
    genres = {slugify(r["genre"]) for r in conn.execute("SELECT DISTINCT genre FROM mixtapes WHERE removed=0 AND genre<>''").fetchall()}
    cities = {slugify(r["city"]) for r in conn.execute("SELECT DISTINCT city FROM users WHERE role='artist' AND city<>''").fetchall()}
    conn.close()
    urls = ["/", "/singles", "/charts", "/get-paid", "/faq", "/wall-of-paid", "/versus", "/genres", "/cities"]
    urls += [f"/tape/{t['id']}" for t in tapes if is_live(dict(t))]
    urls += [(f"/@{a['vanity']}" if a["vanity"] else f"/artist/{a['id']}") for a in artists]
    urls += [f"/genre/{g}" for g in sorted(genres) if g]
    urls += [f"/city/{c}" for c in sorted(cities) if c]
    items = "".join(f"<url><loc>{html.escape(base + u)}</loc></url>" for u in urls)
    return PlainTextResponse(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>',
                             media_type="application/xml")


# ---------------------------------------------------------------- auth routes
@app.get("/signup")
def signup_page(request: Request):
    body = """<h1>Sign up free</h1>
<p><span class="bigfree">FREE TO UPLOAD</span></p>
<form method="post" action="/signup">
<label>Display name <input name="display_name" required></label>
<label>Email <input name="email" type="email" required></label>
<label>Password <input name="password" type="password" required minlength="6"></label>
<label>City / hometown <span class="muted">(optional — fans find you by city)</span> <input name="city" placeholder="Chicago, IL"></label>
<label>I am an <select name="role"><option value="artist">Artist — I post mixtapes</option>
<option value="fan">Fan — I listen, download, tip</option></select></label>
<button class="btn" type="submit">Create free account</button></form>
<p class="muted">Already have an account? <a href="/login">Log in</a></p>"""
    return layout(request, "Sign up", body)


@app.post("/signup")
def signup(request: Request, display_name: str = Form(...), email: str = Form(...),
           password: str = Form(...), role: str = Form(...), city: str = Form("")):
    if role not in ("artist", "fan"):
        return PlainTextResponse("Invalid role", status_code=400)
    email = email.strip().lower()
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return PlainTextResponse("An account with that email already exists", status_code=400)
    pw_hash, pw_salt = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users(email,display_name,role,pw_hash,pw_salt,created_at,city) VALUES(?,?,?,?,?,?,?)",
        (email, display_name.strip(), role, pw_hash, pw_salt, now_iso(), city.strip()))
    if role == "artist":
        base_v = re.sub(r"[^a-z0-9_]+", "_", display_name.strip().lower()).strip("_")
        if len(base_v) >= 3 and base_v not in RESERVED_VANITY:
            taken = conn.execute("SELECT 1 FROM users WHERE vanity=?", (base_v,)).fetchone()
            vanity = base_v if not taken else f"{base_v}{cur.lastrowid}"
            conn.execute("UPDATE users SET vanity=? WHERE id=?", (vanity, cur.lastrowid))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                 (token, cur.lastrowid, now_iso()))
    conn.commit()
    conn.close()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("dh_session", token, httponly=True, samesite="lax")
    resp.set_cookie("dh_anon", secrets.token_urlsafe(12), samesite="lax")
    return resp


@app.get("/login")
def login_page(request: Request):
    body = """<h1>Log in</h1>
<form method="post" action="/login">
<label>Email <input name="email" type="email" required></label>
<label>Password <input name="password" type="password" required></label>
<button class="btn" type="submit">Log in</button></form>
<p class="muted">New here? <a href="/signup">Sign up free</a></p>"""
    return layout(request, "Log in", body)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row or not verify_password(password, row["pw_hash"], row["pw_salt"]):
        conn.close()
        return PlainTextResponse("Wrong email or password", status_code=401)
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                 (token, row["id"], now_iso()))
    conn.commit()
    conn.close()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("dh_session", token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("dh_session")
    if token:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("dh_session")
    return resp


def require_login(request: Request):
    user = current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


# ---------------------------------------------------------------- profile
@app.post("/profile")
def profile(request: Request, city: str = Form(""), vanity: str = Form("")):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("UPDATE users SET city=? WHERE id=?", (city.strip(), user["id"]))
    v = vanity.strip().lower()
    if v and user["role"] == "artist":
        if not re.fullmatch(r"[a-z0-9_]{3,20}", v):
            conn.close()
            return PlainTextResponse("Vanity link must be 3-20 characters: lowercase letters, numbers, underscore.", status_code=400)
        if v in RESERVED_VANITY:
            conn.close()
            return PlainTextResponse("That link name is reserved — pick another.", status_code=400)
        taken = conn.execute("SELECT id FROM users WHERE vanity=?", (v,)).fetchone()
        if taken and taken["id"] != user["id"]:
            conn.close()
            return PlainTextResponse("That vanity link is already taken.", status_code=400)
        conn.execute("UPDATE users SET vanity=? WHERE id=?", (v, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


# ---------------------------------------------------------------- upload
@app.get("/upload")
def upload_page(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    if user["role"] != "artist":
        return PlainTextResponse("Only Artist accounts can upload music.", status_code=403)
    body = """<h1>Post music</h1>
<p><span class="bigfree">FREE TO UPLOAD</span></p>
<p class="muted">No card, no fee. Pro and Sponsored are optional — never required to post or earn.</p>
<form method="post" action="/upload" enctype="multipart/form-data">
<label>What are you dropping?
<select name="kind" id="kind-sel">
<option value="tape">MIXTAPE — a full tape, one or more tracks</option>
<option value="single">SINGLE — just one song</option>
</select></label>
<label><span id="title-label">Mixtape title</span> <input name="title" required></label>
<label>Cover image (jpg / png / webp) <input name="cover" type="file" accept=".jpg,.jpeg,.png,.webp" required></label>
<label>Genre <input name="genre" placeholder="Hip-Hop / Rap"></label>
<label>Hosted by <span class="muted">(optional — a DJ or host credit, shown on your tape)</span> <input name="hosted_by" placeholder="DJ ..."></label>
<label>Premiere — drop it later? <span class="muted">(optional — leave empty to drop NOW. If set, the tape shows a countdown, fans can sign up for a drop alert, and streaming opens at drop time.)</span>
<input type="datetime-local" id="prem-local">
<input type="hidden" name="release_at_utc" id="prem-utc"></label>
<label>Description <textarea name="description" rows="3"></textarea></label>
<label>Download mode
<select name="download_mode">
<option value="free">FREE DOWNLOAD — fans with a free account can download (builds your fan list)</option>
<option value="stream_only">STREAM ONLY — listen on the site only, no downloads for anyone</option>
<option value="paid">PAID DOWNLOAD — stream free, download costs your price</option>
</select></label>
<label>Price if Paid Download ($) <input name="price" type="number" step="0.5" min="1" value="10"></label>
<label><span id="tracks-label">Audio tracks — mp3 / wav / m4a / ogg, up to 40MB each, add at least 1</span>
<input name="tracks" id="tracks-input" type="file" accept=".mp3,.wav,.m4a,.ogg" multiple required></label>
<label style="display:flex;gap:8px;align-items:flex-start">
<input type="checkbox" name="rights" value="yes" style="width:auto;margin-top:3px">
<span>I confirm this music is mine or I have permission to post it. I keep 100% of my rights.</span></label>
<button class="btn" type="submit">Post my music — free</button></form>
<script>
const kindSel = document.getElementById('kind-sel');
function kindToggle(){
  const single = kindSel.value === 'single';
  document.getElementById('title-label').textContent = single ? 'Single title' : 'Mixtape title';
  document.getElementById('tracks-label').textContent = single
    ? 'Your song — one audio file, mp3 / wav / m4a / ogg, up to 40MB'
    : 'Audio tracks — mp3 / wav / m4a / ogg, up to 40MB each, add at least 1';
  const inp = document.getElementById('tracks-input');
  if(single){ inp.removeAttribute('multiple'); } else { inp.setAttribute('multiple',''); }
}
kindSel.addEventListener('change', kindToggle); kindToggle();
document.querySelector('form').addEventListener('submit', () => {
  const v = document.getElementById('prem-local').value;
  document.getElementById('prem-utc').value = v ? new Date(v).toISOString() : '';
});
</script>"""
    return layout(request, "Upload", body, user)


@app.post("/upload")
async def upload(request: Request, title: str = Form(...), genre: str = Form(""),
                 description: str = Form(""), download_mode: str = Form("free"),
                 price: float = Form(0), rights: str = Form(None),
                 hosted_by: str = Form(""), release_at_utc: str = Form(""),
                 kind: str = Form("tape"),
                 cover: UploadFile = File(...), tracks: list[UploadFile] = File(...)):
    user = current_user(request)
    if not user:
        return PlainTextResponse("Log in first", status_code=401)
    if user["role"] != "artist":
        return PlainTextResponse("Only Artist accounts can upload music.", status_code=403)
    if rights != "yes":
        return PlainTextResponse("You must confirm you own this music or have permission to post it (rights attestation required).", status_code=400)
    if download_mode not in ("stream_only", "free", "paid"):
        return PlainTextResponse("Invalid download mode", status_code=400)
    if kind not in ("tape", "single"):
        return PlainTextResponse("Invalid release type", status_code=400)
    if not tracks:
        return PlainTextResponse("Add at least one audio track.", status_code=400)
    if kind == "single" and len(tracks) != 1:
        return PlainTextResponse("A single is exactly one song — upload one track, or post it as a mixtape instead.", status_code=400)
    # validate cover
    cover_ext = Path(cover.filename or "").suffix.lower()
    if cover_ext not in COVER_EXTS:
        return PlainTextResponse("Cover must be a jpg, png or webp image.", status_code=400)
    cover_bytes = await cover.read()
    if len(cover_bytes) > MAX_COVER_BYTES:
        return PlainTextResponse("Cover image is too large (max 8MB).", status_code=400)
    if not sniff_cover(cover_ext, cover_bytes[:16]):
        return PlainTextResponse("Cover file is not a real image of that type.", status_code=400)
    # validate tracks fully before writing anything
    track_data = []
    for t in tracks:
        ext = Path(t.filename or "").suffix.lower()
        if ext not in AUDIO_EXTS:
            return PlainTextResponse(
                f"Track '{t.filename}' is not audio we accept (mp3, wav, m4a, ogg only). Video and other files are rejected.",
                status_code=400)
        data = await t.read()
        if len(data) == 0:
            return PlainTextResponse(f"Track '{t.filename}' is empty.", status_code=400)
        if len(data) > MAX_TRACK_BYTES:
            return PlainTextResponse(f"Track '{t.filename}' is over the 40MB per-track limit.", status_code=400)
        if not sniff_audio(ext, data[:16]):
            return PlainTextResponse(f"Track '{t.filename}' does not look like a real {ext} audio file.", status_code=400)
        track_data.append((t.filename, ext, data))
    price_cents = int(round(price * 100)) if download_mode == "paid" else 0
    if download_mode == "paid" and price_cents < 50:
        return PlainTextResponse("Paid Download price must be at least $0.50.", status_code=400)

    release_at = None
    if release_at_utc.strip():
        try:
            dt = datetime.fromisoformat(release_at_utc.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                release_at = dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            release_at = None
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO mixtapes(artist_id,title,cover_path,genre,description,download_mode,price_cents,created_at,hosted_by,release_at,kind) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], title.strip(), "", genre.strip(), description.strip(),
         download_mode, price_cents, now_iso(), hosted_by.strip()[:60], release_at, kind))
    tape_id = cur.lastrowid
    tape_dir = UPLOAD_ROOT / str(tape_id)
    tape_dir.mkdir(parents=True, exist_ok=True)
    cover_name = f"cover{cover_ext}"
    (tape_dir / cover_name).write_bytes(cover_bytes)
    conn.execute("UPDATE mixtapes SET cover_path=? WHERE id=?", (str(tape_dir / cover_name), tape_id))
    for i, (fname, ext, data) in enumerate(track_data, 1):
        safe = f"track{i}{ext}"
        (tape_dir / safe).write_bytes(data)
        track_title = Path(fname).stem.replace("_", " ").replace("-", " ").strip() or f"Track {i}"
        conn.execute("INSERT INTO tracks(tape_id,title,file_path,track_num) VALUES(?,?,?,?)",
                     (tape_id, track_title, str(tape_dir / safe), i))
    # notify followers
    followers = conn.execute("SELECT follower_id FROM follows WHERE artist_id=?", (user["id"],)).fetchall()
    for f in followers:
        conn.execute("INSERT INTO notifications(user_id,text,tape_id,created_at) VALUES(?,?,?,?)",
                     (f["follower_id"], f"{user['display_name']} just dropped a new {'single' if kind == 'single' else 'tape'}: {title.strip()}", tape_id, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


# ---------------------------------------------------------------- tape / artist pages
@app.get("/cover/{tape_id}")
def cover(tape_id: int):
    conn = get_db()
    row = conn.execute("SELECT cover_path FROM mixtapes WHERE id=?", (tape_id,)).fetchone()
    conn.close()
    if not row or not row["cover_path"] or not Path(row["cover_path"]).exists():
        return FileResponse(str(BASE_DIR / "static" / "hitz-logo.jpg"), media_type="image/jpeg")
    ext = Path(row["cover_path"]).suffix.lower()
    return FileResponse(row["cover_path"], media_type=MEDIA_TYPES.get(ext, "image/jpeg"))


@app.get("/tape/{tape_id}")
def tape_page(request: Request, tape_id: int):
    conn = get_db()
    sweep_premieres(conn)
    user = current_user(request)
    row = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.id=?",
        (tape_id,)).fetchone()
    if not row or row["removed"]:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    tape = dict(row)
    owner = bool(user) and user["id"] == tape["artist_id"]
    live = is_live(tape)
    golden_id = golden_tape_id(conn)
    _wk = chart_rows(conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), limit=1)
    winner_id = _wk[0][1]["id"] if _wk and _wk[0][0] > 0 else None
    s = tape_stats(conn, tape_id)
    followers = conn.execute("SELECT COUNT(*) c FROM follows WHERE artist_id=?", (tape["artist_id"],)).fetchone()["c"]
    base = site_base(request)
    og = {"title": f"{tape['title']} — {tape['artist_name']}",
          "desc": (f"{s['plays']} plays · {s['downloads']} downloads · {s['likes']} likes on HITZ, The Real Mixtape Connect. "
                   f"{mode_badge(tape)}. Artists upload free and get paid — 80% of sales, 90% of tips."),
          "image": base + f"/cover/{tape_id}",
          "url": base + f"/tape/{tape_id}"}
    jsonld = ("<script type=\"application/ld+json\">" + json.dumps({
        "@context": "https://schema.org", "@type": "MusicAlbum",
        "name": tape["title"], "byArtist": {"@type": "MusicGroup", "name": tape["artist_name"]},
        "image": base + f"/cover/{tape_id}", "url": base + f"/tape/{tape_id}",
        "genre": tape["genre"] or "Hip-Hop"}) + "</script>")
    hosted_line = (f"<p class='muted'>\U0001F3A7 Hosted by {html.escape(tape['hosted_by'])}</p>"
                   if tape.get("hosted_by") else "")
    embed_code = (f'<iframe src="{base}/embed/{tape_id}" width="100%" height="250" frameborder="0" '
                  f'allow="autoplay" style="border:1px solid #c9a227;border-radius:10px"></iframe>')
    embed_section = f"""<details><summary>\U0001F4E4 Embed this tape on your site / link-in-bio</summary>
<p class="muted">Copy this player code — every play out there still counts here, and the player sends fans back to HITZ.</p>
<textarea class="code" id="embed-code" rows="3" readonly>{html.escape(embed_code)}</textarea>
<button class="btn secondary small" type="button" onclick="navigator.clipboard.writeText(document.getElementById('embed-code').value).then(()=>alert('Embed code copied'))">Copy embed code</button></details>"""
    qr_section = f"""<details><summary>\U0001F4F7 QR code — print it for flyers & posters</summary>
<p class="muted">Scan it and this tape opens. Put it on show flyers, stickers, posters.</p>
<img src="/qr/tape/{tape_id}" alt="QR code for this tape" style="width:140px;height:140px;background:#fff;padding:6px;border-radius:8px">
<p><a href="/qr/tape/{tape_id}" download="hitz-tape-{tape_id}-qr.svg">Download the QR image</a></p></details>"""
    golden_hero = ('<img class="goldentape" src="/static/golden-tape.jpg" alt="GOLDEN TAPE" '
                   'title="GOLDEN TAPE - the most-played tape on HITZ">') if golden_id == tape_id else ""
    golden_pill = ('<span class="badge gold">\U0001F947 GOLDEN TAPE — most plays on HITZ</span> ') if golden_id == tape_id else ""
    if tape.get("kind") == "single":
        golden_pill = '<span class="badge gold">\U0001F3B5 SINGLE</span> ' + golden_pill
        if golden_single_id(conn) == tape_id:
            golden_hero = ('<img class="goldentape" src="/static/golden-tape.jpg" alt="GOLDEN SINGLE" '
                           'title="GOLDEN SINGLE - the most-played single on HITZ">')
            golden_pill = ('<span class="badge gold">\U0001F3B5 SINGLE</span> '
                           '<span class="badge gold">\U0001F947 GOLDEN SINGLE — most plays on HITZ</span> ')

    if not live and not owner:
        subs = conn.execute("SELECT COUNT(*) c FROM premiere_notify WHERE tape_id=?", (tape_id,)).fetchone()["c"]
        on_list = bool(user) and bool(conn.execute(
            "SELECT 1 FROM premiere_notify WHERE tape_id=? AND user_id=?", (tape_id, user["id"])).fetchone())
        if not user:
            notify_block = '<p><a class="btn" href="/login">Log in to get the drop alert</a></p>'
        elif on_list:
            notify_block = '<p class="ok">\u2713 You are on the list — we will alert you the second it drops.</p>'
        else:
            notify_block = (f'<form method="post" action="/premiere-notify/{tape_id}">'
                            '<button class="btn" type="submit">\U0001F514 Notify Me When It Drops</button></form>')
        countdown_js = """<script>
const target = new Date("__REL__").getTime();
function tick(){
  let d = Math.max(0, target - Date.now());
  const days = Math.floor(d/86400000), hrs = Math.floor(d%86400000/3600000),
        min = Math.floor(d%3600000/60000), sec = Math.floor(d%60000/1000);
  document.getElementById('cd').textContent = days + 'd ' + hrs + 'h ' + min + 'm ' + sec + 's';
  if(d <= 0){ location.reload(); }
}
tick(); setInterval(tick, 1000);
</script>""".replace("__REL__", tape["release_at"])
        body = f"""
<span class="tapehero"><img src="/cover/{tape_id}" alt="cover" style="width:220px;height:220px;object-fit:cover;border-radius:12px">{golden_hero}</span>
<h1>{html.escape(tape['title'])}</h1>
<p>by <a href="/artist/{tape['artist_id']}">{html.escape(tape['artist_name'])}</a> · {html.escape(tape['genre'] or '')} · {followers} followers</p>
{hosted_line}
<p><span class="badge">\U0001F552 PREMIERE</span></p>
<p style="font-size:1.15rem">This tape drops <b class="gold">{tape['release_at'][:16].replace('T', ' ')} UTC</b></p>
<div class="countdown" id="cd">…</div>
<p class="muted">{subs} fan(s) waiting on this drop. Streaming opens the second it lands — no downloads or plays before drop time, for anyone.</p>
{notify_block}
<p>{html.escape(tape['description'] or '')}</p>
{embed_section}
{jsonld}
{countdown_js}
{SHARE_JS}"""
        conn.close()
        return layout(request, tape["title"], body, user, og=og)

    tracks = conn.execute("SELECT * FROM tracks WHERE tape_id=? ORDER BY track_num", (tape_id,)).fetchall()
    js_tracks = json.dumps([{"id": t["id"], "title": t["title"], "artist": tape["artist_name"]} for t in tracks])
    track_lines = "".join(
        f"""<div class="trackline"><button class="btn small" onclick='dhPlay({js_tracks},{i},{tape_id})'>\u25B6</button>
<span>{i+1}. {html.escape(t['title'])}</span></div>""" for i, t in enumerate(tracks))
    if tape["download_mode"] == "stream_only":
        dl = '<p><span class="badge dark">STREAM ONLY</span> <span class="muted">Downloads are off for this tape — the artist\'s setting always wins.</span></p>'
    elif tape["download_mode"] == "free":
        dl = (f'<p><a class="btn" href="/download/{tape_id}">\u2B07 Free Download</a> '
              '<span class="muted">Free Fan account required — downloading follows this artist.</span></p>')
    else:
        dl = (f'<form method="post" action="/buy-download/{tape_id}" style="display:inline">'
              f'<button class="btn" type="submit">\u2B07 Buy Download — {fmt(tape["price_cents"])}</button></form> '
              f'<a class="btn secondary" href="/download/{tape_id}">I already paid — download</a>'
              f'<p class="muted">You keep streaming free. The artist keeps 80% (85% on Pro). {money_label()}.</p>')
    premiere_banner = ""
    if not live and owner:
        premiere_banner = (f"<div class='ok'>\U0001F552 PREMIERE — this tape drops {tape['release_at'][:16].replace('T', ' ')} UTC. "
                           "Only you can preview it until then.</div>")
    vspill = ('<span class="badge gold">\u2694\uFE0F VERSUS WINNER</span> '
              if tape_won_versus(conn, tape_id) else "")
    # playlists: add-to-playlist form
    playlist_block = ""
    if user:
        pls = conn.execute("SELECT * FROM playlists WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        if pls:
            opts = "".join(f"<option value='{p['id']}'>{html.escape(p['title'])}</option>" for p in pls)
            playlist_block = (f'<form method="post" action="/playlist-add" style="display:inline">'
                              f'<input type="hidden" name="tape_id" value="{tape_id}">'
                              f'<select name="playlist_id" style="width:auto">{opts}</select>'
                              '<button class="btn secondary small" type="submit">+ Add to playlist</button></form>')
        else:
            playlist_block = '<a class="btn secondary small" href="/playlists">Create a playlist</a>'
    # versus: start-a-battle form for the owner
    versus_block = ""
    if owner:
        active = conn.execute("SELECT 1 FROM battles WHERE (tape_a=? OR tape_b=?) AND ends_at>?",
                              (tape_id, tape_id, now_iso())).fetchone()
        if active:
            versus_block = '<p class="muted">\u2694\uFE0F This tape is in an active Versus battle — <a href="/versus">watch it</a>.</p>'
        else:
            opps = conn.execute(
                "SELECT m.id, m.title, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id "
                "WHERE m.removed=0 AND m.id<>? AND (m.release_at IS NULL OR m.release_at<=?) "
                "ORDER BY m.created_at DESC LIMIT 100", (tape_id, now_iso())).fetchall()
            if opps:
                opts = "".join(f"<option value='{o['id']}'>{html.escape(o['title'])} — {html.escape(o['artist_name'])}</option>" for o in opps)
                versus_block = (f'<details><summary>\u2694\uFE0F Start a Versus battle with this tape</summary>'
                                '<p class="muted">7 days. Fans vote. Winner takes the Versus crown on their tape. The other artist gets challenged by alert.</p>'
                                f'<form method="post" action="/versus/start"><input type="hidden" name="tape_a" value="{tape_id}">'
                                f'<label>Opponent tape <select name="tape_b">{opts}</select></label>'
                                '<button class="btn small" type="submit">Start the battle</button></form></details>')
    # comments
    comment_rows = conn.execute(
        "SELECT c.*, u.display_name FROM comments c JOIN users u ON u.id=c.user_id "
        "WHERE c.tape_id=? ORDER BY c.created_at DESC LIMIT 100", (tape_id,)).fetchall()
    top_tip = conn.execute(
        "SELECT payer_id, SUM(amount_cents) tot FROM transactions WHERE artist_id=? AND kind='tip' "
        "AND status='paid' AND payer_id IS NOT NULL GROUP BY payer_id ORDER BY tot DESC LIMIT 1",
        (tape["artist_id"],)).fetchone()
    top_supporter_id = top_tip["payer_id"] if top_tip else None
    comment_lines = ""
    for c in comment_rows:
        sup = ' <span class="badge gold">\u2B50 TOP SUPPORTER</span>' if c["user_id"] == top_supporter_id else ""
        del_form = (f'<form method="post" action="/comment/{c["id"]}/delete" style="display:inline">'
                    '<button class="btn secondary small" type="submit">Delete</button></form>'
                    if user and c["user_id"] == user["id"] else "")
        comment_lines += (f"<div class='card' style='margin:8px 0'><b>{html.escape(c['display_name'])}</b>{sup} "
                          f"<span class='muted'>{html.escape(c['created_at'][:10])}</span>"
                          f"<p style='margin:6px 0'>{html.escape(c['body'])}</p>{del_form}</div>")
    if user:
        comment_form = (f'<form method="post" action="/comment/{tape_id}">'
                        '<label>Say something <input name="body" maxlength="500" required placeholder="This tape is..."></label>'
                        '<button class="btn small" type="submit">Post comment</button></form>')
    else:
        comment_form = '<p class="muted"><a href="/login">Log in</a> to comment.</p>'
    ov_block = ""
    if tape.get("open_verse"):
        entries = conn.execute(
            "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city "
            "FROM verse_entries e JOIN mixtapes m ON m.id=e.entry_tape_id JOIN users u ON u.id=m.artist_id "
            "WHERE e.original_tape_id=? AND m.removed=0 ORDER BY e.created_at", (tape_id,)).fetchall()
        entry_cards = "".join(tape_card(conn, dict(e), winner_id, golden_id) for e in entries)
        link_form = ""
        if user and user["role"] == "artist" and user["id"] != tape["artist_id"]:
            entered = {r["entry_tape_id"] for r in conn.execute("SELECT entry_tape_id FROM verse_entries").fetchall()}
            mine = [m for m in conn.execute(
                "SELECT id, title FROM mixtapes WHERE artist_id=? AND removed=0 AND id<>? "
                "AND (release_at IS NULL OR release_at<=?) ORDER BY created_at DESC",
                (user["id"], tape_id, now_iso())).fetchall() if m["id"] not in entered]
            if mine:
                opts = "".join(f"<option value='{m['id']}'>{html.escape(m['title'])}</option>" for m in mine)
                link_form = (f'<form method="post" action="/verse-entry"><input type="hidden" name="original_id" value="{tape_id}">'
                             f'<label>Your response tape <select name="entry_id">{opts}</select></label>'
                             '<button class="btn small" type="submit">\U0001F3A4 Enter this Open Verse</button></form>')
        note_html = f"<p class='muted'>{html.escape(tape['open_verse_note'])}</p>" if tape.get("open_verse_note") else ""
        ov_block = (f"<details open><summary>\U0001F3A4 OPEN VERSE — jump on this tape</summary>{note_html}{link_form}"
                    f"<h4>Entries ({len(entries)})</h4><div class='grid'>"
                    f"{entry_cards or '<p class=muted>No entries yet — be the first.</p>'}</div></details>")
    entry_banner = ""
    _ent = conn.execute(
        "SELECT o.id, o.title FROM verse_entries e JOIN mixtapes o ON o.id=e.original_tape_id "
        "WHERE e.entry_tape_id=?", (tape_id,)).fetchone()
    if _ent:
        entry_banner = (f"<div class='ok'>\U0001F3A4 This tape is an Open Verse entry for "
                        f"<a href='/tape/{_ent['id']}'>{html.escape(_ent['title'])}</a></div>")
    rec_list = [dict(r) for r in conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city "
        "FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.removed=0 AND m.id<>? "
        "AND (m.release_at IS NULL OR m.release_at<=?) AND m.genre=? ORDER BY m.created_at DESC LIMIT 4",
        (tape_id, now_iso(), tape["genre"] or "")).fetchall()]
    if len(rec_list) < 4:
        _have = {r["id"] for r in rec_list} | {tape_id}
        for _, _t, _s in chart_rows(conn, limit=12):
            if len(rec_list) >= 4:
                break
            if _t["id"] not in _have:
                rec_list.append(_t)
                _have.add(_t["id"])
    rec_section = ""
    if rec_list:
        rec_cards = "".join(tape_card(conn, t, winner_id, golden_id) for t in rec_list)
        rec_section = f"<h3 style='margin-top:22px'>Fans Also Played</h3><div class='grid'>{rec_cards}</div>"
    body = f"""
<span class="tapehero"><img src="/cover/{tape_id}" alt="cover" style="width:220px;height:220px;object-fit:cover;border-radius:12px">{golden_hero}</span>
<h1>{html.escape(tape['title'])}</h1>
<p>by <a href="/artist/{tape['artist_id']}">{html.escape(tape['artist_name'])}</a> · {html.escape(tape['genre'] or '')} · {followers} followers</p>
{hosted_line}
{premiere_banner}{entry_banner}
<p>{golden_pill}{vspill}<span class="badge dark">{mode_badge(tape)}</span></p>
<p class="muted" data-plays="{s['plays']}" data-downloads="{s['downloads']}" data-likes="{s['likes']}">
\u25B6 {s['plays']} plays · \u2B07 {s['downloads']} downloads · \u2665 {s['likes']} likes</p>
<p>{html.escape(tape['description'] or '')}</p>
{track_lines}
<div style="margin:16px 0">{dl}</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:14px 0;align-items:center">
<form method="post" action="/like/{tape_id}"><button class="btn secondary small" type="submit">\u2665 Like</button></form>
<form method="post" action="/follow/{tape['artist_id']}"><button class="btn secondary small" type="submit">Follow Artist</button></form>
<button class="btn secondary small" onclick="dhShare('{html.escape(tape['artist_name'], quote=True)}','{html.escape(tape['title'], quote=True)}')">Share</button>
{playlist_block}
</div>
{embed_section}
{qr_section}
{versus_block}
{ov_block}
<h3>Tip the artist — they keep 90%</h3>
<form method="post" action="/tip/{tape_id}">
<label>Tip amount ($) <input name="amount" type="number" step="0.5" min="0.5" value="5" style="max-width:160px"></label>
<button class="btn small" type="submit">Tip $1 / $5 / $10 — any amount</button>
<span class="muted">{money_label()}.</span></form>
<h3 style="margin-top:22px">Comments</h3>
{comment_form}
{comment_lines or '<p class="muted">No comments yet — start the conversation.</p>'}
{rec_section}
<h3 style="margin-top:22px">Report</h3>
<form method="post" action="/report/{tape_id}">
<label>Reason (copyright / DMCA / other) <input name="reason" required></label>
<button class="btn secondary small" type="submit">Report this tape</button></form>
{jsonld}
{SHARE_JS}"""
    conn.close()
    return layout(request, tape["title"], body, user, og=og)


@app.get("/artist/{artist_id}")
def artist_page(request: Request, artist_id: int):
    conn = get_db()
    a = conn.execute("SELECT * FROM users WHERE id=? AND role='artist'", (artist_id,)).fetchone()
    if not a:
        conn.close()
        return PlainTextResponse("Artist not found", status_code=404)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=50)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    tapes = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.artist_id=? AND m.removed=0 ORDER BY m.created_at DESC", (artist_id,)).fetchall()
    followers = conn.execute("SELECT COUNT(*) c FROM follows WHERE artist_id=?", (artist_id,)).fetchone()["c"]
    cards = "".join(tape_card(conn, dict(t), winner_id, golden_id) for t in tapes) or "<p class='muted'>No tapes yet.</p>"
    pro = ' <span class="badge">PRO ✓ VERIFIED</span>' if a["is_pro"] else ""
    city_html = (f"<p class='muted'>\U0001F4CD <a href='/city/{slugify(a['city'])}'>{html.escape(a['city'])}</a></p>"
                 if a["city"] else "")
    vanity_html = (f"<p class='muted'>\U0001F517 hitzmixtapes.com/@{html.escape(a['vanity'])}</p>"
                   if a["vanity"] else "")
    sups = conn.execute(
        "SELECT u.display_name, SUM(t.amount_cents) tot FROM transactions t JOIN users u ON u.id=t.payer_id "
        "WHERE t.artist_id=? AND t.kind='tip' AND t.status='paid' AND t.payer_id IS NOT NULL "
        "GROUP BY t.payer_id ORDER BY tot DESC LIMIT 3", (artist_id,)).fetchall()
    sup_block = ""
    if sups:
        _lines = "".join(
            f"<div class='card' style='margin:6px 0'><b>{html.escape(su['display_name'])}</b> "
            f"<span class='badge gold'>\u2B50 TOP SUPPORTER</span> "
            f"<span class='muted'>tipped {fmt(su['tot'])} total</span></div>" for su in sups)
        sup_block = f"<h3>\u2B50 Top Supporters</h3>{_lines}"
    qr_html = (f"<p class='muted'><img src='/qr/artist/{artist_id}' alt='QR code for this artist' "
               "style='width:96px;height:96px;background:#fff;padding:5px;border-radius:8px;vertical-align:middle'> "
               "Scan to open this page — flyers, posters, everywhere.</p>")
    body = f"""<h1>{html.escape(a['display_name'])}{pro}</h1>
{city_html}{vanity_html}
<p class="muted">{followers} followers</p>
<form method="post" action="/follow/{artist_id}"><button class="btn small" type="submit">Follow Artist</button></form>
<form method="post" action="/subscribe/{artist_id}" style="margin-top:8px">
<button class="btn secondary small" type="submit">Subscribe — {fmt(SUB_PRICE_CENTS)}/mo (artist keeps 80%)</button></form>
{sup_block}
{qr_html}
<h2>Tapes</h2><div class="grid">{cards}</div>{SHARE_JS}"""
    conn.close()
    base = site_base(request)
    og = {"title": f"{a['display_name']} — HITZ",
          "desc": f"{a['display_name']} on HITZ, The Real Mixtape Connect. {followers} followers. Stream the tapes free.",
          "image": base + "/static/hitz-logo.jpg",
          "url": base + (f"/@{a['vanity']}" if a["vanity"] else f"/artist/{artist_id}")}
    return layout(request, a["display_name"], body, og=og)



# ---------------------------------------------------------------- comments / playlists / premieres / versus
@app.post("/premiere-notify/{tape_id}")
def premiere_notify_route(request: Request, tape_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO premiere_notify(tape_id,user_id,created_at) VALUES(?,?,?)",
                 (tape_id, user["id"], now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/comment/{tape_id}")
def comment(request: Request, tape_id: int, body: str = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    text = body.strip()
    if not text or len(text) > 500:
        return PlainTextResponse("Comment must be 1-500 characters.", status_code=400)
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    if not is_live(dict(tape)) and tape["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("Comments open when this tape drops.", status_code=403)
    conn.execute("INSERT INTO comments(tape_id,user_id,body,created_at) VALUES(?,?,?,?)",
                 (tape_id, user["id"], text, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/comment/{comment_id}/delete")
def comment_delete(request: Request, comment_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    c = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not c:
        conn.close()
        return PlainTextResponse("Comment not found", status_code=404)
    if c["user_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("You can only delete your own comments.", status_code=403)
    conn.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{c['tape_id']}", status_code=303)


@app.get("/playlists")
def playlists_page(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    mine = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id) n "
        "FROM playlists p WHERE p.user_id=? ORDER BY p.created_at DESC", (user["id"],)).fetchall()
    others = conn.execute(
        "SELECT p.*, u.display_name owner_name, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id) n "
        "FROM playlists p JOIN users u ON u.id=p.user_id WHERE p.user_id<>? ORDER BY p.created_at DESC LIMIT 12",
        (user["id"],)).fetchall()
    conn.close()
    my_lines = "".join(
        f"<div class='card' style='margin:8px 0'><a href='/playlist/{p['id']}'><b>{html.escape(p['title'])}</b></a> "
        f"<span class='muted'>{p['n']} tape(s)</span></div>" for p in mine)
    other_lines = "".join(
        f"<div class='card' style='margin:8px 0'><a href='/playlist/{p['id']}'><b>{html.escape(p['title'])}</b></a> "
        f"<span class='muted'>by {html.escape(p['owner_name'])} · {p['n']} tape(s)</span></div>" for p in others)
    body = f"""<h1>Playlists</h1>
<form method="post" action="/playlists"><label>New playlist name <input name="title" required maxlength="80"></label>
<button class="btn small" type="submit">Create playlist</button></form>
<h2>Your playlists</h2>{my_lines or '<p class="muted">None yet — create one, then add tapes from any tape page.</p>'}
<h2 style="margin-top:26px">Fresh from other fans</h2>{other_lines or '<p class="muted">No public playlists yet.</p>'}"""
    return layout(request, "Playlists", body, user)


@app.post("/playlists")
def playlists_create(request: Request, title: str = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("INSERT INTO playlists(user_id,title,created_at) VALUES(?,?,?)",
                 (user["id"], title.strip()[:80], now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse("/playlists", status_code=303)


@app.get("/playlist/{playlist_id}")
def playlist_page(request: Request, playlist_id: int):
    user = current_user(request)
    conn = get_db()
    sweep_premieres(conn)
    p = conn.execute("SELECT p.*, u.display_name owner_name FROM playlists p JOIN users u ON u.id=p.user_id WHERE p.id=?",
                     (playlist_id,)).fetchone()
    if not p:
        conn.close()
        return PlainTextResponse("Playlist not found", status_code=404)
    rows = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city "
        "FROM playlist_items i JOIN mixtapes m ON m.id=i.tape_id JOIN users u ON u.id=m.artist_id "
        "WHERE i.playlist_id=? AND m.removed=0 ORDER BY i.created_at", (playlist_id,)).fetchall()
    weekly = chart_rows(conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), limit=50)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    golden_id = golden_tape_id(conn)
    cards = "".join(tape_card(conn, dict(r), winner_id, golden_id) for r in rows)
    owner = bool(user) and user["id"] == p["user_id"]
    manage = ""
    if owner:
        items = "".join(
            f"<form method='post' action='/playlist-remove' style='display:inline-block;margin:3px'>"
            f"<input type='hidden' name='playlist_id' value='{playlist_id}'><input type='hidden' name='tape_id' value='{r['id']}'>"
            f"<button class='btn secondary small' type='submit'>Remove '{html.escape(r['title'])}'</button></form>" for r in rows)
        manage = (f"<h3>Manage</h3>{items}"
                  f"<form method='post' action='/playlist-delete' style='margin-top:10px'>"
                  f"<input type='hidden' name='playlist_id' value='{playlist_id}'>"
                  "<button class='btn secondary small' type='submit'>Delete this playlist</button></form>")
    base = site_base(request)
    conn.close()
    body = f"""<h1>{html.escape(p['title'])}</h1>
<p class="muted">Playlist by {html.escape(p['owner_name'])} · {len(rows)} tape(s) · Share it: {base}/playlist/{playlist_id}</p>
<div class="grid">{cards or '<p class="muted">No tapes yet.</p>'}</div>
{manage}{SHARE_JS}"""
    return layout(request, p["title"], body, user)


@app.post("/playlist-add")
def playlist_add(request: Request, playlist_id: int = Form(...), tape_id: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    p = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not p or p["user_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("That is not your playlist.", status_code=403)
    conn.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,tape_id,created_at) VALUES(?,?,?)",
                 (playlist_id, tape_id, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/playlist-remove")
def playlist_remove(request: Request, playlist_id: int = Form(...), tape_id: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    p = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not p or p["user_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("That is not your playlist.", status_code=403)
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=? AND tape_id=?", (playlist_id, tape_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)


@app.post("/playlist-delete")
def playlist_delete(request: Request, playlist_id: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    p = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not p or p["user_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("That is not your playlist.", status_code=403)
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/playlists", status_code=303)


@app.get("/versus")
def versus_page(request: Request):
    user = current_user(request)
    conn = get_db()
    sweep_battles(conn)
    battles = conn.execute("SELECT * FROM battles ORDER BY created_at DESC LIMIT 30").fetchall()

    def side(b, tid, votes, my_vote):
        t = conn.execute(
            "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.id=?",
            (tid,)).fetchone()
        if not t:
            return "<div class='card'>Tape removed</div>"
        total = max(1, votes.get(b["tape_a"], 0) + votes.get(b["tape_b"], 0))
        pct = round(100 * votes.get(tid, 0) / total)
        active = b["ends_at"] > now_iso()
        winner = battle_winner(conn, b)
        crown = " \U0001F3C6 WINNER" if winner == tid else ""
        if not user:
            btn = '<a class="btn small" href="/login">Log in to vote</a>'
        elif not active:
            btn = '<span class="muted">Voting closed</span>'
        elif my_vote == tid:
            btn = '<span class="badge gold">\u2713 Your pick</span>'
        elif my_vote:
            btn = '<span class="muted">You voted for the other tape</span>'
        else:
            btn = (f'<form method="post" action="/versus/{b["id"]}/vote">'
                   f'<input type="hidden" name="tape_id" value="{tid}">'
                   '<button class="btn small" type="submit">Vote</button></form>')
        return (f"<div class='card' style='flex:1;min-width:230px'>"
                f"<a href='/tape/{tid}'><img class='cover' src='/cover/{tid}' alt='cover' style='width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px'></a>"
                f"<div style='margin-top:8px'><a href='/tape/{tid}' style='font-weight:800;color:#fff'>{html.escape(t['title'])}</a>{crown}</div>"
                f"<div class='muted'>by {html.escape(t['artist_name'])}</div>"
                f"<div style='font-size:1.25rem;margin-top:6px'><b class='gold'>{votes.get(tid, 0)}</b> votes</div>"
                f"<div class='bar'><i style='width:{pct}%'></i></div>{btn}</div>")

    active_html, done_html = "", ""
    for b in battles:
        votes = battle_votes(conn, b["id"])
        my_vote = None
        if user:
            v = conn.execute("SELECT tape_id FROM battle_votes WHERE battle_id=? AND user_id=?",
                             (b["id"], user["id"])).fetchone()
            my_vote = v["tape_id"] if v else None
        block = (f"<div style='display:flex;gap:14px;flex-wrap:wrap;margin:14px 0'>"
                 f"{side(b, b['tape_a'], votes, my_vote)}"
                 f"<div style='align-self:center;font-weight:800;font-size:1.3rem' class='gold'>VS</div>"
                 f"{side(b, b['tape_b'], votes, my_vote)}</div>")
        if b["ends_at"] > now_iso():
            active_html += f"<p class='muted'>Ends {b['ends_at'][:10]} — one vote per fan. Make it count.</p>" + block
        else:
            w = battle_winner(conn, b)
            verdict = "Ended in a TIE" if not w else "Winner decided"
            done_html += f"<p class='muted'>{verdict} · ended {b['ends_at'][:10]}</p>" + block
    conn.close()
    body = f"""<h1>\u2694\uFE0F Versus Battles</h1>
<p>Two tapes. Seven days. Fans vote. The winner wears the Versus crown on their tape — and both artists get a crowd they didn't have before. Artists start a battle from their own tape page.</p>
<h2>Live battles</h2>{active_html or '<p class="muted">No live battles right now — an artist can start one from their tape page.</p>'}
<h2 style="margin-top:28px">Finished</h2>{done_html or '<p class="muted">No finished battles yet.</p>'}"""
    return layout(request, "Versus", body, user)


@app.post("/versus/start")
def versus_start(request: Request, tape_a: int = Form(...), tape_b: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    if tape_a == tape_b:
        return PlainTextResponse("A tape cannot battle itself.", status_code=400)
    conn = get_db()
    a = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_a,)).fetchone()
    b = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_b,)).fetchone()
    if not a or not b:
        conn.close()
        return PlainTextResponse("Tape not found", status_code=404)
    if a["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("You can only send YOUR tape into battle.", status_code=403)
    if not is_live(dict(a)) or not is_live(dict(b)):
        conn.close()
        return PlainTextResponse("Both tapes must be live (not premieres) to battle.", status_code=400)
    busy = conn.execute(
        "SELECT 1 FROM battles WHERE ends_at>? AND (tape_a IN (?,?) OR tape_b IN (?,?))",
        (now_iso(), tape_a, tape_b, tape_a, tape_b)).fetchone()
    if busy:
        conn.close()
        return PlainTextResponse("One of these tapes is already in an active battle.", status_code=400)
    ends = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    cur = conn.execute("INSERT INTO battles(tape_a,tape_b,created_by,ends_at,created_at) VALUES(?,?,?,?,?)",
                       (tape_a, tape_b, user["id"], ends, now_iso()))
    notify(conn, b["artist_id"],
           f"\u2694\uFE0F CHALLENGE: '{a['title']}' just called out your tape '{b['title']}' — 7-day Versus battle is LIVE!",
           tape_b)
    conn.commit()
    conn.close()
    return RedirectResponse("/versus", status_code=303)


@app.post("/versus/{battle_id}/vote")
def versus_vote(request: Request, battle_id: int, tape_id: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    b = conn.execute("SELECT * FROM battles WHERE id=?", (battle_id,)).fetchone()
    if not b:
        conn.close()
        return PlainTextResponse("Battle not found", status_code=404)
    if b["ends_at"] <= now_iso():
        conn.close()
        return PlainTextResponse("Voting has closed for this battle.", status_code=400)
    if tape_id not in (b["tape_a"], b["tape_b"]):
        conn.close()
        return PlainTextResponse("That tape is not in this battle.", status_code=400)
    conn.execute("INSERT OR IGNORE INTO battle_votes(battle_id,user_id,tape_id,created_at) VALUES(?,?,?,?)",
                 (battle_id, user["id"], tape_id, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse("/versus", status_code=303)




# ---------------------------------------------------------------- radio / QR / open verse
@app.get("/radio")
def radio_page(request: Request):
    conn = get_db()
    sweep_premieres(conn)
    rows = conn.execute(
        "SELECT t.id track_id, t.title track_title, m.id tape_id, m.title tape_title, m.genre "
        "FROM tracks t JOIN mixtapes m ON m.id=t.tape_id "
        "WHERE m.removed=0 AND (m.release_at IS NULL OR m.release_at<=?) ORDER BY m.created_at DESC",
        (now_iso(),)).fetchall()
    names = {r["id"]: r for r in conn.execute(
        "SELECT m.id, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id").fetchall()}

    def item(r):
        return {"id": r["track_id"], "title": r["track_title"], "tape": r["tape_id"],
                "artist": (names[r["tape_id"]]["artist_name"] if r["tape_id"] in names else "") + " — " + r["tape_title"]}

    all_items = [item(r) for r in rows]
    stations = [("all", "\U0001F4FB HITZ Radio — Everything", all_items)]
    by_genre = {}
    for r in rows:
        if r["genre"]:
            by_genre.setdefault(slugify(r["genre"]), (r["genre"], []))[1].append(item(r))
    for slug, (gname, items) in sorted(by_genre.items()):
        stations.append((f"g-{slug}", f"\U0001F3B6 {gname} Radio", items))
    chart = chart_rows(conn, limit=10)
    chart_ids = [t["id"] for _, t, _ in chart]
    by_tape = {}
    for it in all_items:
        by_tape.setdefault(it["tape"], []).append(it)
    golden_items = [it for tid in chart_ids for it in by_tape.get(tid, [])]
    if golden_items:
        stations.append(("golden", "\U0001F947 Golden Radio — Top Tapes", golden_items))
    stations.append(("new", "\U0001F195 Newest Drops Radio", all_items))
    conn.close()
    data = {k: items for k, _, items in stations}
    buttons = "".join(
        f"<button class='btn' style='margin:4px' onclick=\"dhRadio('{k}')\">\u25B6 {label} ({len(items)})</button>"
        for k, label, items in stations if items)
    body = f"""<h1>\U0001F4FB HITZ Radio</h1>
<p>Nonstop mixtapes. Pick a station and let it ride — every play counts for the artist, same as pressing play on the tape.</p>
<p>{buttons or '<span class="muted">No tapes on the station yet — the first drop starts the radio.</span>'}</p>
<p class="muted">Stations shuffle fresh every time you press play. Golden Radio plays the all-time chart leaders back to back.</p>
<script>
const STATIONS = {json.dumps(data)};
function dhRadio(k){{
  const q = STATIONS[k].slice();
  for(let i = q.length - 1; i > 0; i--){{ const j = Math.floor(Math.random() * (i + 1)); [q[i], q[j]] = [q[j], q[i]]; }}
  if(q.length){{ dhPlay(q, 0, null); }}
}}
</script>"""
    return layout(request, "HITZ Radio", body)


def make_qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string().decode()


@app.get("/qr/tape/{tape_id}")
def qr_tape(request: Request, tape_id: int):
    conn = get_db()
    row = conn.execute("SELECT id FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    conn.close()
    if not row:
        return PlainTextResponse("Mixtape not found", status_code=404)
    return PlainTextResponse(make_qr_svg(f"{site_base(request)}/tape/{tape_id}"), media_type="image/svg+xml")


@app.get("/qr/artist/{artist_id}")
def qr_artist(request: Request, artist_id: int):
    conn = get_db()
    row = conn.execute("SELECT id, vanity FROM users WHERE id=? AND role='artist'", (artist_id,)).fetchone()
    conn.close()
    if not row:
        return PlainTextResponse("Artist not found", status_code=404)
    path = f"/@{row['vanity']}" if row["vanity"] else f"/artist/{artist_id}"
    return PlainTextResponse(make_qr_svg(site_base(request) + path), media_type="image/svg+xml")


@app.post("/tape/{tape_id}/open-verse")
def open_verse_toggle(request: Request, tape_id: int, open: str = Form("0"), note: str = Form("")):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=?", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    if tape["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("Only the tape's artist can open a verse on it.", status_code=403)
    conn.execute("UPDATE mixtapes SET open_verse=?, open_verse_note=? WHERE id=?",
                 (1 if open == "1" else 0, note.strip()[:120], tape_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/verse-entry")
def verse_entry(request: Request, original_id: int = Form(...), entry_id: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    if user["role"] != "artist":
        return PlainTextResponse("Open Verse entries are for Artist accounts.", status_code=403)
    if original_id == entry_id:
        return PlainTextResponse("A tape cannot be its own entry.", status_code=400)
    conn = get_db()
    orig = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (original_id,)).fetchone()
    entry = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (entry_id,)).fetchone()
    if not orig or not entry:
        conn.close()
        return PlainTextResponse("Tape not found", status_code=404)
    if entry["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("You can only enter your own tape.", status_code=403)
    if not orig["open_verse"] or not is_live(dict(orig)) or not is_live(dict(entry)):
        conn.close()
        return PlainTextResponse("That tape is not taking Open Verse entries right now.", status_code=400)
    try:
        conn.execute("INSERT INTO verse_entries(original_tape_id,entry_tape_id,created_at) VALUES(?,?,?)",
                     (original_id, entry_id, now_iso()))
    except sqlite3.IntegrityError:
        conn.close()
        return PlainTextResponse("That tape is already entered in an Open Verse.", status_code=400)
    notify(conn, orig["artist_id"],
           f"\U0001F3A4 NEW OPEN VERSE ENTRY: {user['display_name']} jumped on your tape '{orig['title']}' with '{entry['title']}'!",
           entry_id)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{original_id}", status_code=303)



# ---------------------------------------------------------------- streaming / plays
def iter_file_range(path: Path, start: int, end: int, chunk=64 * 1024):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@app.get("/stream/{track_id}")
def stream(request: Request, track_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT t.*, m.removed, m.release_at, m.artist_id FROM tracks t JOIN mixtapes m ON m.id=t.tape_id WHERE t.id=?",
        (track_id,)).fetchone()
    conn.close()
    if not row or row["removed"]:
        return PlainTextResponse("Track not found", status_code=404)
    if row["release_at"] and row["release_at"] > now_iso():
        viewer = current_user(request)
        if not viewer or viewer["id"] != row["artist_id"]:
            return PlainTextResponse(
                f"This tape premieres on {row['release_at'][:10]} — streaming opens when it drops.",
                status_code=403)
    path = Path(row["file_path"])
    if not path.exists():
        return PlainTextResponse("Audio file missing", status_code=404)
    size = path.stat().st_size
    media = MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg")
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            rng = range_header[6:].split(",")[0]
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                return PlainTextResponse("Range not satisfiable", status_code=416)
        except ValueError:
            return PlainTextResponse("Bad range", status_code=400)
        headers = {"Content-Range": f"bytes {start}-{end}/{size}",
                   "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        return StreamingResponse(iter_file_range(path, start, end), status_code=206,
                                 headers=headers, media_type=media)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
    return StreamingResponse(iter_file_range(path, 0, size - 1), headers=headers, media_type=media)


@app.post("/play/{tape_id}")
def record_play(request: Request, tape_id: int):
    user = current_user(request)
    key = listener_key(request, user)
    conn = get_db()
    tape = conn.execute("SELECT id, release_at FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return JSONResponse({"counted": False}, status_code=404)
    if tape["release_at"] and tape["release_at"] > now_iso():
        conn.close()
        return JSONResponse({"counted": False, "premiere": True}, status_code=403)
    exists = conn.execute(
        "SELECT 1 FROM events WHERE tape_id=? AND kind='play' AND listener_key=? AND day=?",
        (tape_id, key, today_str())).fetchone()
    counted = False
    if not exists:
        conn.execute("INSERT INTO events(tape_id,kind,listener_key,day,created_at) VALUES(?,?,?,?,?)",
                     (tape_id, "play", key, today_str(), now_iso()))
        conn.commit()
        counted = True
    plays = conn.execute("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='play'",
                         (tape_id,)).fetchone()["c"]
    conn.close()
    return {"counted": counted, "plays": plays}


# ---------------------------------------------------------------- downloads
def build_tape_zip(conn, tape) -> bytes:
    tracks = conn.execute("SELECT * FROM tracks WHERE tape_id=? ORDER BY track_num", (tape["id"],)).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tracks:
            p = Path(t["file_path"])
            if p.exists():
                z.write(p, arcname=f"{t['track_num']:02d} - {t['title']}{p.suffix.lower()}")
    return buf.getvalue()


def count_download_once(conn, tape_id: int, key: str):
    exists = conn.execute(
        "SELECT 1 FROM events WHERE tape_id=? AND kind='download' AND listener_key=?",
        (tape_id, key)).fetchone()
    if not exists:
        conn.execute("INSERT INTO events(tape_id,kind,listener_key,day,created_at) VALUES(?,?,?,?,?)",
                     (tape_id, "download", key, today_str(), now_iso()))
        return True
    return False


def auto_follow(conn, follower_id: int, artist_id: int):
    if follower_id == artist_id:
        return
    conn.execute("INSERT OR IGNORE INTO follows(follower_id,artist_id,created_at) VALUES(?,?,?)",
                 (follower_id, artist_id, now_iso()))


@app.get("/download/{tape_id}")
def download(request: Request, tape_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    tape = dict(tape)
    if not is_live(tape):
        conn.close()
        return PlainTextResponse(
            f"This tape has not dropped yet — it premieres on {tape['release_at'][:10]}.",
            status_code=403)
    if tape["download_mode"] == "stream_only":
        conn.close()
        return PlainTextResponse(
            "This tape is STREAM ONLY. Downloads are off for everyone, including Premium — the artist's setting always wins.",
            status_code=403)
    if tape["download_mode"] == "paid":
        paid = conn.execute(
            "SELECT 1 FROM transactions WHERE kind='paid_download' AND tape_id=? AND payer_id=? AND status='paid'",
            (tape_id, user["id"])).fetchone()
        if not paid:
            conn.close()
            return PlainTextResponse(
                "This is a PAID DOWNLOAD tape. Buy the download first — it counts after your purchase is fulfilled.",
                status_code=403)
    # Paid downloads count under a separate key from free downloads: a fan who
    # grabbed a tape while it was free still counts once more when they buy it.
    key = f"user:{user['id']}" + (":paid" if tape["download_mode"] == "paid" else "")
    if tape["download_mode"] == "free":
        auto_follow(conn, user["id"], tape["artist_id"])
    count_download_once(conn, tape_id, key)
    conn.commit()
    data = build_tape_zip(conn, tape)
    conn.close()
    fname = "".join(c for c in tape["title"] if c.isalnum() or c in " -_").strip() or "mixtape"
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}.zip"'})


# ---------------------------------------------------------------- social
@app.post("/like/{tape_id}")
def like(request: Request, tape_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO likes(user_id,tape_id,created_at) VALUES(?,?,?)",
                 (user["id"], tape_id, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/follow/{artist_id}")
def follow(request: Request, artist_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    auto_follow(conn, user["id"], artist_id)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/artist/{artist_id}", status_code=303)


@app.get("/notifications")
def notifications(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    rows = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                        (user["id"],)).fetchall()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    items = "".join(
        f"<div class='card' style='margin:8px 0'>{html.escape(r['text'])} "
        f"{'<a href=\"/tape/' + str(r['tape_id']) + '\">Listen →</a>' if r['tape_id'] else ''}"
        f"<div class='muted'>{html.escape(r['created_at'][:10])}</div></div>" for r in rows)
    return layout(request, "Alerts", f"<h1>Alerts</h1>{items or '<p class=\"muted\">No alerts yet. Follow an artist and you will see their drops here.</p>'}", user)


@app.post("/report/{tape_id}")
def report(request: Request, tape_id: int, reason: str = Form(...)):
    user = current_user(request)
    conn = get_db()
    conn.execute("INSERT INTO reports(tape_id,reporter_id,reason,created_at) VALUES(?,?,?,?)",
                 (tape_id, user["id"] if user else None, reason.strip(), now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


# ---------------------------------------------------------------- money flows
def create_tx(conn, kind, payer_id, artist_id, tape_id, amount_cents, artist_cents, meta=""):
    platform = amount_cents - artist_cents
    cur = conn.execute(
        "INSERT INTO transactions(kind,payer_id,artist_id,tape_id,amount_cents,artist_cents,platform_cents,status,meta,created_at) "
        "VALUES(?,?,?,?,?,?,?, 'unpaid_request', ?, ?)",
        (kind, payer_id, artist_id, tape_id, amount_cents, artist_cents, platform, meta, now_iso()))
    return cur.lastrowid


def split_for(artist_row, kind, amount_cents):
    if kind == "tip":
        return int(round(amount_cents * 0.90))
    if kind == "paid_download":
        return int(round(amount_cents * (0.85 if artist_row and artist_row["is_pro"] else 0.80)))
    if kind == "subscription":
        return int(round(amount_cents * 0.80))
    return 0


@app.post("/tip/{tape_id}")
def tip(request: Request, tape_id: int, amount: float = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    cents = int(round(amount * 100))
    if cents < 50:
        return PlainTextResponse("Tip must be at least $0.50", status_code=400)
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    artist = conn.execute("SELECT * FROM users WHERE id=?", (tape["artist_id"],)).fetchone()
    create_tx(conn, "tip", user["id"], tape["artist_id"], tape_id, cents,
              split_for(artist, "tip", cents), meta="tip")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/buy-download/{tape_id}")
def buy_download(request: Request, tape_id: int, amount: float = Form(None)):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape or tape["download_mode"] != "paid":
        conn.close()
        return PlainTextResponse("This tape is not a Paid Download tape.", status_code=400)
    cents = int(round(amount * 100)) if amount else tape["price_cents"]
    if cents < tape["price_cents"]:
        conn.close()
        return PlainTextResponse("Amount is below the artist's download price (you may pay more, never less).", status_code=400)
    artist = conn.execute("SELECT * FROM users WHERE id=?", (tape["artist_id"],)).fetchone()
    create_tx(conn, "paid_download", user["id"], tape["artist_id"], tape_id, cents,
              split_for(artist, "paid_download", cents), meta="paid_download")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/tape/{tape_id}", status_code=303)


@app.post("/subscribe/{artist_id}")
def subscribe(request: Request, artist_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    artist = conn.execute("SELECT * FROM users WHERE id=? AND role='artist'", (artist_id,)).fetchone()
    if not artist:
        conn.close()
        return PlainTextResponse("Artist not found", status_code=404)
    create_tx(conn, "subscription", user["id"], artist_id, None, SUB_PRICE_CENTS,
              split_for(artist, "subscription", SUB_PRICE_CENTS), meta="fan_sub_monthly")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/artist/{artist_id}", status_code=303)


@app.post("/sponsored/{tape_id}")
def sponsored_req(request: Request, tape_id: int, days: int = Form(...)):
    user, redir = require_login(request)
    if redir:
        return redir
    if days not in SPONSORED_TIERS:
        return PlainTextResponse("Pick 3, 7 or 14 days", status_code=400)
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape or tape["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("You can only sponsor your own tape.", status_code=403)
    create_tx(conn, "sponsored", user["id"], user["id"], tape_id, SPONSORED_TIERS[days], 0,
              meta=f"sponsored_{days}d")
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/pro-request")
def pro_request(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    if user["role"] != "artist":
        return PlainTextResponse("Artist Pro is for Artist accounts.", status_code=403)
    conn = get_db()
    create_tx(conn, "pro", user["id"], user["id"], None, PRO_PRICE_CENTS, 0, meta="artist_pro_monthly")
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/premium-request")
def premium_request(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    create_tx(conn, "premium", user["id"], None, None, PREMIUM_PRICE_CENTS, 0, meta="fan_premium_monthly")
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/sponsored-spots")
def sponsored_spots():
    conn = get_db()
    active = len(sponsored_active(conn))
    conn.close()
    return {"total": SPONSORED_MAX_ACTIVE, "active": active,
            "left": max(0, SPONSORED_MAX_ACTIVE - active)}


# ---------------------------------------------------------------- HITZ RIPS trading cards
# Buy a pack -> rip it -> reveal 5 real artist cards. Every card is a real
# opted-in, charting HITZ artist + their top tape. 40% of every PAID pack is
# the Artist Pool, split among the pulled artists by card value.
# Money honesty: packs follow the site-wide rule — a buy saves as an
# unpaid_request ("payments being connected", no charge) and a pack can only
# be ripped after its transaction is marked paid by Admin fulfillment
# (payment verified by Ken), exactly like paid downloads. Free SAMPLE packs
# carry $0 value, pay no artist pool, and can never be sold back.
RIPS_PACK_PRICE_CENTS = 1000
RIPS_PACK_SIZE = 5
RIPS_POOL_CENTS = 400  # 40% of the $10 pack price
RIPS_TIER_VALUES = {"Legend": 7500, "Superstar": 2000, "Star": 800, "Rising": 300, "Rookie": 100}
RIPS_TIER_WEIGHTS = {"Rookie": 55, "Rising": 25, "Star": 12, "Superstar": 6, "Legend": 2}
RIPS_TIER_ORDER = ["Legend", "Superstar", "Star", "Rising", "Rookie"]


def rips_tier_for_rank(rank: int) -> str:
    if rank <= 1:
        return "Legend"
    if rank <= 3:
        return "Superstar"
    if rank <= 10:
        return "Star"
    if rank <= 25:
        return "Rising"
    return "Rookie"


def rips_eligible(conn):
    """Card-eligible artists: opted in AND charting with a real score > 0.
    Artist score = sum of their tapes' chart scores (plays + 2x downloads +
    3x likes). The card carries the artist's top-scoring tape."""
    by_artist = {}
    for score, tape, stats in chart_rows(conn, limit=100000):
        a = by_artist.get(tape["artist_id"])
        if a is None:
            a = by_artist[tape["artist_id"]] = {
                "artist_id": tape["artist_id"], "artist_name": tape["artist_name"],
                "artist_vanity": tape.get("artist_vanity"), "score": 0, "top_score": -1,
                "tape_id": None, "tape_title": "", "cover_path": None,
                "stats": {"plays": 0, "downloads": 0, "likes": 0}}
        a["score"] += score
        if score > a["top_score"]:
            a["top_score"] = score
            a["tape_id"] = tape["id"]
            a["tape_title"] = tape["title"]
            a["cover_path"] = tape["cover_path"]
            a["stats"] = stats
    out = []
    for a in by_artist.values():
        if a["score"] <= 0:
            continue
        u = conn.execute("SELECT cards_opt_in FROM users WHERE id=? AND role='artist'",
                         (a["artist_id"],)).fetchone()
        if not u or not u["cards_opt_in"]:
            continue
        out.append(a)
    out.sort(key=lambda x: (-x["score"], -x["stats"]["plays"], x["artist_id"]))
    for i, a in enumerate(out, 1):
        a["rank"] = i
        a["tier"] = rips_tier_for_rank(i)
        a["value_cents"] = RIPS_TIER_VALUES[a["tier"]]
    return out


def rips_tier_pool(eligible):
    tiers = {}
    for e in eligible:
        tiers.setdefault(e["tier"], []).append(e)
    return tiers


def rips_draw(eligible, n=RIPS_PACK_SIZE):
    """Server-side draw: each card independently picks a tier by the posted
    weights (redistributed across tiers actually present), then a random
    artist inside that tier. Duplicates allowed, like real packs."""
    tiers = rips_tier_pool(eligible)
    present = [(t, RIPS_TIER_WEIGHTS[t]) for t in RIPS_TIER_ORDER if t in tiers]
    total = sum(w for _, w in present)
    picks = []
    for _ in range(n):
        r = secrets.randbelow(total)
        acc = 0
        chosen = present[-1][0]
        for t, w in present:
            acc += w
            if r < acc:
                chosen = t
                break
        picks.append(secrets.choice(tiers[chosen]))
    return picks


def rips_pool_shares(picks):
    """Split the Artist Pool by card value. Shares sum EXACTLY to the pool:
    floor shares first, leftover cents go to the highest-value cards."""
    total_value = sum(p["value_cents"] for p in picks)
    shares = [RIPS_POOL_CENTS * p["value_cents"] // total_value for p in picks]
    rem = RIPS_POOL_CENTS - sum(shares)
    order = sorted(range(len(picks)), key=lambda i: (-picks[i]["value_cents"], i))
    k = 0
    while rem > 0:
        shares[order[k % len(order)]] += 1
        rem -= 1
        k += 1
    return shares


def rips_execute(conn, user, tx):
    """Rip one pack transaction exactly once. The caller MUST hold a
    BEGIN IMMEDIATE transaction and commit after. Returns the new card ids,
    or None if the pack was already ripped or the pool is empty.
    Paid packs disperse the Artist Pool here — inside the same transaction
    as the ripped_at stamp, so a re-rip can never double-pay."""
    fresh = conn.execute("SELECT * FROM transactions WHERE id=?", (tx["id"],)).fetchone()
    if fresh is None or fresh["ripped_at"]:
        return None
    eligible = rips_eligible(conn)
    if not eligible:
        return None
    is_sample = 1 if fresh["kind"] == "card_pack_sample" else 0
    picks = rips_draw(eligible)
    card_ids = []
    for e in picks:
        cur = conn.execute(
            "INSERT INTO user_cards(user_id,artist_id,tape_id,tier,value_cents,txn_id,is_sample,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user["id"], e["artist_id"], e["tape_id"], e["tier"],
             0 if is_sample else e["value_cents"], fresh["id"], is_sample, now_iso()))
        card_ids.append(cur.lastrowid)
    if not is_sample:
        for e, share in zip(picks, rips_pool_shares(picks)):
            conn.execute(
                "INSERT INTO transactions(kind,payer_id,artist_id,tape_id,amount_cents,artist_cents,"
                "platform_cents,status,meta,created_at,fulfilled_at) "
                "VALUES('card_pool',?,?,?,?,?,0,'paid',?,?,?)",
                (user["id"], e["artist_id"], e["tape_id"], share, share,
                 json.dumps({"pack_txn": fresh["id"], "tier": e["tier"]}), now_iso(), now_iso()))
    conn.execute("UPDATE transactions SET ripped_at=? WHERE id=?", (now_iso(), fresh["id"]))
    return card_ids


def rips_card_html(card, show_sellback=False):
    cover = f"/cover/{card['tape_id']}" if card["cover_path"] else "/static/hitz-logo.jpg"
    sample = ' <span class="badge silver">SAMPLE</span>' if card["is_sample"] else ""
    sold = ' <span class="badge silver">SOLD BACK</span>' if card["sold_back"] else ""
    value = "Value $0.00 — SAMPLE" if card["is_sample"] else f"Value {fmt(card['value_cents'])}"
    sell = ""
    if show_sellback and not card["is_sample"] and not card["sold_back"]:
        sell = (f'<form method="post" action="/rips/sellback/{card["id"]}" style="margin-top:8px">'
                f'<button class="btn secondary small" type="submit">Sell back for {fmt(card["value_cents"])} platform credit</button></form>')
    if show_sellback and card["is_sample"]:
        sell = '<p class="muted" style="margin-top:8px">Sample cards have no sell-back value.</p>'
    return f"""<div class="card">
<img class="cover" src="{cover}" alt="cover">
<div style="margin-top:8px"><b style="color:#fff">{html.escape(card['artist_name'])}</b></div>
<div class="muted">“{html.escape(card['tape_title'])}”</div>
<div><span class="badge gold">{html.escape(card['tier'].upper())}</span>{sample}{sold}</div>
<div class="muted" style="margin-top:6px">{value}</div>
<div class="muted">▶ {card['plays']} plays · ⬇ {card['downloads']} downloads · ♥ {card['likes']} likes</div>
{sell}</div>"""


def rips_cards_for_txn(conn, txn_id):
    return conn.execute(
        "SELECT uc.*, m.title tape_title, m.cover_path cover_path, u.display_name artist_name, "
        "(SELECT COUNT(*) FROM events e WHERE e.tape_id=uc.tape_id AND e.kind='play') plays, "
        "(SELECT COUNT(*) FROM events e WHERE e.tape_id=uc.tape_id AND e.kind='download') downloads, "
        "(SELECT COUNT(*) FROM likes l WHERE l.tape_id=uc.tape_id) likes "
        "FROM user_cards uc JOIN mixtapes m ON m.id=uc.tape_id JOIN users u ON u.id=uc.artist_id "
        "WHERE uc.txn_id=? ORDER BY uc.id", (txn_id,)).fetchall()


@app.get("/rips")
def rips_page(request: Request):
    user = current_user(request)
    conn = get_db()
    eligible = rips_eligible(conn)
    tiers = rips_tier_pool(eligible)
    total_w = sum(RIPS_TIER_WEIGHTS[t] for t in tiers)
    rank_lines = ""
    for e in eligible:
        href = f"/@{e['artist_vanity']}" if e["artist_vanity"] else f"/artist/{e['artist_id']}"
        rank_lines += (f"<tr><td>#{e['rank']}</td><td><a href='{href}'>{html.escape(e['artist_name'])}</a></td>"
                       f"<td>{html.escape(e['tape_title'])}</td><td>{e['score']}</td>"
                       f"<td><span class='badge gold'>{e['tier'].upper()}</span></td><td>{fmt(e['value_cents'])}</td></tr>")
    rankings = (f"<table><tr><th>#</th><th>Artist</th><th>Top tape</th><th>HITZ Score</th><th>Tier</th><th>Card value</th></tr>{rank_lines}</table>"
                if eligible else
                "<div class='card' style='text-align:center;padding:24px'><h3>Series 1 hasn't dropped yet</h3>"
                "<p class='muted'>The rankings fill up the moment the first artists opt in and chart. "
                "Artists: turn on “Put me on HITZ Rips trading cards” in your Dashboard, then get plays, downloads and likes.</p></div>")
    odds_lines = ""
    for t in RIPS_TIER_ORDER:
        if t in tiers:
            pct = RIPS_TIER_WEIGHTS[t] / total_w * 100
            odds_lines += (f"<tr><td><span class='badge gold'>{t.upper()}</span></td><td>{len(tiers[t])} artist(s)</td>"
                           f"<td>{fmt(RIPS_TIER_VALUES[t])}</td><td>{pct:.1f}% per card</td></tr>")
    odds = (f"<table><tr><th>Tier</th><th>In the pool</th><th>Card value</th><th>Odds</th></tr>{odds_lines}</table>"
            "<p class='muted'>Odds are computed live from the artists actually in the pool right now, using the posted tier weights "
            "(Rookie 55 · Rising 25 · Star 12 · Superstar 6 · Legend 2, redistributed across the tiers present). "
            "Each of the 5 cards in a pack is drawn independently — duplicates can happen, like real packs.</p>"
            if eligible else "<p class='muted'>Pack odds appear here as soon as the first artists are in the pool.</p>")
    my_packs = ""
    sample_html = ""
    if user:
        packs = conn.execute(
            "SELECT * FROM transactions WHERE payer_id=? AND kind='card_pack' ORDER BY created_at DESC LIMIT 10",
            (user["id"],)).fetchall()
        lines = ""
        for p in packs:
            if p["ripped_at"]:
                action = f"<a class='btn small secondary' href='/rips/reveal/{p['id']}'>View your rip</a>"
                state = "Ripped"
            elif p["status"] == "paid":
                action = (f"<form method='post' action='/rips/rip/{p['id']}' style='display:inline'>"
                          "<button class='btn small' type='submit'>\U0001F0CF RIP IT NOW</button></form>")
                state = "Paid — ready to rip"
            else:
                action = "<span class='muted'>Unlocks when payment is verified</span>"
                state = "Saved — payments being connected, no charge made yet"
            lines += (f"<div class='card' style='margin:8px 0'><b>Series 1 Pack</b> · {fmt(p['amount_cents'])} · {state} "
                      f"<span class='muted'>{p['created_at'][:10]}</span><div style='margin-top:6px'>{action}</div></div>")
        if lines:
            my_packs = f"<h2>Your packs</h2>{lines}"
        has_sample = conn.execute(
            "SELECT 1 FROM transactions WHERE payer_id=? AND kind='card_pack_sample'",
            (user["id"],)).fetchone()
        if not has_sample:
            sample_html = (f"<form method='post' action='/rips/sample' style='margin-top:10px'>"
                           "<button class='btn secondary' type='submit'>Rip ONE free SAMPLE pack</button></form>"
                           "<p class='muted'>Every account gets one free sample rip. Sample cards are marked SAMPLE, "
                           "worth $0, pay no artist pool and can't be sold back.</p>") if eligible else \
                          "<p class='muted'>Free sample packs open when Series 1 drops.</p>"
        else:
            sample_html = "<p class='muted'>You've ripped your free sample pack.</p>"
    else:
        sample_html = "<p class='muted'><a href='/login'>Log in</a> to buy a pack or rip your one free sample pack.</p>"
    buy_html = (f"<form method='post' action='/rips/buy'>"
                "<button class='btn' type='submit'>Buy a Series 1 Pack — $10.00</button></form>"
                f"<p class='muted'>{money_label()}.</p>") if eligible else \
               "<p><b>Packs aren't on sale yet</b> — Series 1 drops when the first artists opt in and chart.</p>"
    body = f"""<h1>\U0001F0CF HITZ RIPS — Trading Cards</h1>
<p>Rip. Reveal. Triumph — HITZ style. Buy a pack, rip it open and pull <b>real HITZ artists</b> as trading cards:
the artist + their top tape's cover, ranked by real plays, downloads and likes. <b>Every artist whose card you pull
gets paid</b> out of the pack you bought.</p>
<h2>\U0001F4CA Artist Power Rankings</h2>
<p class="muted">Ranked by HITZ Score (plays + 2× downloads + 3× likes, all tapes combined). Only artists who opted in
and are charting get a card. Tiers by rank: #1 Legend {fmt(7500)} · #2–3 Superstar {fmt(2000)} · #4–10 Star {fmt(800)} ·
#11–25 Rising {fmt(300)} · #26+ Rookie {fmt(100)}.</p>
{rankings}
<h2>\U0001F4E6 Series 1 Pack — $10 · 5 cards</h2>
{odds}
<div class="card"><h3>Where the $10 goes</h3>
<p><b>40% Artist Pool</b> — split among the artists whose cards you pull, weighted by card value<br>
<b>30% HITZ</b> — runs the platform<br>
<b>20% Printing + shipping fund</b> — for physical cards<br>
<b>10% Chase fund</b> — backs the big rare pulls</p>
{buy_html}
{sample_html}
<p style="margin-top:10px"><a href="/rips/my">Your card collection →</a></p></div>
{my_packs}
<h2>How it works</h2>
<div class="card"><p><b>1. Buy a pack</b> — while payments are being connected your request is saved and no charge is made;
the pack unlocks the moment payment is verified.</p>
<p><b>2. Rip it</b> — 5 cards are drawn server-side at the live posted odds.</p>
<p><b>3. Reveal + collect</b> — cards land in <a href="/rips/my">your collection</a> with the artist, cover, tier, value and tape stats.</p>
<p><b>4. Artists eat</b> — the $4.00 Artist Pool from your pack is split among the 5 pulled artists by card value,
and shows up in their HITZ earnings like tips and sales.</p>
<p><b>5. Sell back or hold</b> — sell any paid card back for its full value in <b>platform credit</b> (not cash),
or hold it in your collection. Physical shipping comes later, backed by the printing + shipping fund.</p>
<p class="muted">Artists: opt in from your <a href="/dashboard">Dashboard</a> — “Put me on HITZ Rips trading cards.”</p></div>"""
    conn.close()
    return layout(request, "HITZ Rips", body, user)


@app.post("/rips/opt-in")
def rips_opt_in(request: Request, opt_in: str = Form("0")):
    user, redir = require_login(request)
    if redir:
        return redir
    if user["role"] != "artist":
        return PlainTextResponse("Trading cards are for Artist accounts.", status_code=403)
    conn = get_db()
    conn.execute("UPDATE users SET cards_opt_in=? WHERE id=?",
                 (1 if opt_in == "1" else 0, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/rips/buy")
def rips_buy(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    if not rips_eligible(conn):
        conn.close()
        return PlainTextResponse(
            "Series 1 packs aren't on sale yet — no artists are in the card pool. "
            "Packs drop when the first artists opt in and chart.", status_code=400)
    create_tx(conn, "card_pack", user["id"], None, None, RIPS_PACK_PRICE_CENTS, 0, meta="series1_pack")
    conn.commit()
    conn.close()
    return RedirectResponse("/rips", status_code=303)


@app.post("/rips/sample")
def rips_sample(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM transactions WHERE payer_id=? AND kind='card_pack_sample'",
            (user["id"],)).fetchone()
        if existing:
            conn.rollback()
            conn.close()
            return PlainTextResponse("You've already ripped your one free sample pack.", status_code=400)
        cur = conn.execute(
            "INSERT INTO transactions(kind,payer_id,artist_id,tape_id,amount_cents,artist_cents,"
            "platform_cents,status,meta,created_at,fulfilled_at) "
            "VALUES('card_pack_sample',?,NULL,NULL,0,0,0,'paid','free_sample_pack',?,?)",
            (user["id"], now_iso(), now_iso()))
        tx = conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone()
        card_ids = rips_execute(conn, user, tx)
        if not card_ids:
            conn.rollback()
            conn.close()
            return PlainTextResponse(
                "Sample packs open when Series 1 drops — no artists are in the card pool yet.", status_code=400)
        conn.commit()
        tx_id = tx["id"]
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return RedirectResponse(f"/rips/reveal/{tx_id}", status_code=303)


@app.post("/rips/rip/{tx_id}")
def rips_rip(request: Request, tx_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not tx or tx["kind"] != "card_pack":
            conn.rollback()
            conn.close()
            return PlainTextResponse("Pack not found.", status_code=404)
        if tx["payer_id"] != user["id"]:
            conn.rollback()
            conn.close()
            return PlainTextResponse("That pack isn't yours.", status_code=403)
        if tx["ripped_at"]:
            conn.rollback()
            conn.close()
            return RedirectResponse(f"/rips/reveal/{tx_id}", status_code=303)
        if tx["status"] != "paid":
            conn.rollback()
            conn.close()
            return PlainTextResponse(
                "This pack isn't unlocked yet — payment hasn't been verified. "
                "While payments are being connected, requests are saved and no charge is made.", status_code=400)
        card_ids = rips_execute(conn, user, tx)
        if not card_ids:
            conn.rollback()
            conn.close()
            return PlainTextResponse(
                "This pack can't be ripped right now — the card pool is empty.", status_code=400)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return RedirectResponse(f"/rips/reveal/{tx_id}", status_code=303)


@app.get("/rips/reveal/{tx_id}")
def rips_reveal(request: Request, tx_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx or tx["kind"] not in ("card_pack", "card_pack_sample") or tx["payer_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("Pack not found.", status_code=404)
    cards = rips_cards_for_txn(conn, tx_id)
    conn.close()
    if not cards:
        return RedirectResponse("/rips", status_code=303)
    grid = "".join(rips_card_html(c) for c in cards)
    sample_note = ("<div class='ok'>This was your <b>free SAMPLE pack</b> — cards are marked SAMPLE, worth $0, "
                   "no artist pool was paid and sample cards can't be sold back.</div>"
                   if tx["kind"] == "card_pack_sample" else
                   "<div class='ok'>The $4.00 Artist Pool from this pack was split among these artists by card value — "
                   "it lands in their HITZ earnings.</div>")
    body = f"""<h1>\U0001F0CF Your rip</h1>
{sample_note}
<div class="grid">{grid}</div>
<p style="margin-top:14px"><a class="btn secondary" href="/rips/my">See your collection</a>
<a class="btn secondary" href="/rips">Back to Rips</a></p>"""
    return layout(request, "Your rip", body, user)


@app.get("/rips/my")
def rips_my(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    cards = conn.execute(
        "SELECT uc.*, m.title tape_title, m.cover_path cover_path, u.display_name artist_name, "
        "(SELECT COUNT(*) FROM events e WHERE e.tape_id=uc.tape_id AND e.kind='play') plays, "
        "(SELECT COUNT(*) FROM events e WHERE e.tape_id=uc.tape_id AND e.kind='download') downloads, "
        "(SELECT COUNT(*) FROM likes l WHERE l.tape_id=uc.tape_id) likes "
        "FROM user_cards uc JOIN mixtapes m ON m.id=uc.tape_id JOIN users u ON u.id=uc.artist_id "
        "WHERE uc.user_id=? ORDER BY uc.created_at DESC, uc.id DESC", (user["id"],)).fetchall()
    total_value = sum(c["value_cents"] for c in cards if not c["is_sample"] and not c["sold_back"])
    credit = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE payer_id=? AND kind='card_sellback_credit'",
        (user["id"],)).fetchone()["s"]
    conn.close()
    grid = "".join(rips_card_html(c, show_sellback=True) for c in cards)
    body = f"""<h1>\U0001F0CF Your card collection</h1>
<div class="grid">
<div class="card"><h3>Collection value</h3><p style="font-size:1.5rem">{fmt(total_value)}</p>
<p class="muted">Face value of your unsold paid cards. Sample cards are worth $0.</p></div>
<div class="card"><h3>Platform credit</h3><p style="font-size:1.5rem">{fmt(credit)}</p>
<p class="muted">Credit earned from sell-backs. Platform credit is not cash.</p></div>
<div class="card"><h3>Cards</h3><p style="font-size:1.5rem">{len(cards)}</p>
<p class="muted"><a href="/rips">Rip more packs →</a></p></div>
</div>
<div class="grid" style="margin-top:14px">{grid or '<div class="card" style="text-align:center;padding:24px"><h3>No cards yet</h3><p class="muted">Rip a pack and your cards land here.</p><a class="btn" href="/rips">Go to Rips</a></div>'}</div>"""
    return layout(request, "Your cards", body, user)


@app.post("/rips/sellback/{card_id}")
def rips_sellback(request: Request, card_id: int):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        card = conn.execute("SELECT * FROM user_cards WHERE id=?", (card_id,)).fetchone()
        if not card or card["user_id"] != user["id"]:
            conn.rollback()
            conn.close()
            return PlainTextResponse("Card not found.", status_code=404)
        if card["is_sample"] or card["value_cents"] <= 0:
            conn.rollback()
            conn.close()
            return PlainTextResponse("Sample cards have no sell-back value.", status_code=400)
        if card["sold_back"]:
            conn.rollback()
            conn.close()
            return PlainTextResponse("That card was already sold back.", status_code=400)
        conn.execute("UPDATE user_cards SET sold_back=1 WHERE id=?", (card_id,))
        conn.execute(
            "INSERT INTO transactions(kind,payer_id,artist_id,tape_id,amount_cents,artist_cents,"
            "platform_cents,status,meta,created_at) "
            "VALUES('card_sellback_credit',?,NULL,?,?,0,0,'credit',?,?)",
            (user["id"], card["tape_id"], card["value_cents"],
             json.dumps({"card_id": card_id, "credit": "platform_credit_not_cash"}), now_iso()))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return RedirectResponse("/rips/my", status_code=303)


# ---------------------------------------------------------------- artist dashboard
@app.get("/dashboard")
def dashboard(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    earn_kinds = ("tip", "paid_download", "subscription", "card_pool")
    paid_rows = conn.execute(
        f"SELECT kind, SUM(artist_cents) s, COUNT(*) n FROM transactions WHERE artist_id=? AND status='paid' AND kind IN {earn_kinds} GROUP BY kind",
        (user["id"],)).fetchall()
    pend_rows = conn.execute(
        f"SELECT kind, SUM(artist_cents) s, COUNT(*) n FROM transactions WHERE artist_id=? AND status IN ('unpaid_request','waitlist') AND kind IN {earn_kinds} GROUP BY kind",
        (user["id"],)).fetchall()
    paid_total = sum(r["s"] or 0 for r in paid_rows)
    pend_total = sum(r["s"] or 0 for r in pend_rows)
    my_requests = conn.execute(
        "SELECT * FROM transactions WHERE payer_id=? ORDER BY created_at DESC LIMIT 30",
        (user["id"],)).fetchall()
    followers = 0
    pro_nudge = ""
    tapes_html = ""
    rips_html = ""
    if user["role"] == "artist":
        followers = conn.execute("SELECT COUNT(*) c FROM follows WHERE artist_id=?", (user["id"],)).fetchone()["c"]
        sales = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE artist_id=? AND kind='paid_download' AND status='paid'",
            (user["id"],)).fetchone()["s"]
        if sales > 0 and not user["is_pro"]:
            extra = int(round(sales * 0.85)) - int(round(sales * 0.80))
            pro_nudge = (f"<div class='ok'><b>Pro math from your own sales:</b> on Artist Pro (85/15) you would have kept "
                         f"<b>{fmt(extra)} more</b> on your {fmt(sales)} of download sales so far. Pro is {fmt(PRO_PRICE_CENTS)}/mo — optional, never required.</div>")
        tapes = conn.execute("SELECT * FROM mixtapes WHERE artist_id=? AND removed=0 ORDER BY created_at DESC",
                             (user["id"],)).fetchall()
        lines = ""
        for t in tapes:
            s = tape_stats(conn, t["id"])
            lines += f"""<div class="card" style="margin:10px 0"><b>{html.escape(t['title'])}</b>
<span class="badge dark">{mode_badge(t)}</span>
<div class="muted">▶ {s['plays']} · ⬇ {s['downloads']} · ♥ {s['likes']}</div>
<form method="post" action="/tape/{t['id']}/mode" style="margin-top:8px">
<label>Download mode <select name="mode">
<option value="stream_only" {'selected' if t['download_mode']=='stream_only' else ''}>STREAM ONLY</option>
<option value="free" {'selected' if t['download_mode']=='free' else ''}>FREE DOWNLOAD</option>
<option value="paid" {'selected' if t['download_mode']=='paid' else ''}>PAID DOWNLOAD</option></select></label>
<label>Price ($) if Paid <input name="price" type="number" step="0.5" min="1" value="{(t['price_cents'] or 1000)/100:g}" style="max-width:140px"></label>
<button class="btn small" type="submit">Save mode</button></form>
<form method="post" action="/tape/{t['id']}/open-verse" style="margin-top:6px">
<label>Open Verse challenge <select name="open">
<option value="0" {'selected' if not t['open_verse'] else ''}>Off</option>
<option value="1" {'selected' if t['open_verse'] else ''}>ON — other artists can jump on this tape</option></select></label>
<label>Note for challengers <input name="note" value="{html.escape(t['open_verse_note'] or '')}" placeholder="Verse 2 is open — 16 bars"></label>
<button class="btn secondary small" type="submit">Save Open Verse</button></form>
<form method="post" action="/sponsored/{t['id']}" style="margin-top:6px">
<label>Sponsor this tape <select name="days"><option value="3">$49 — 3 days</option>
<option value="7">$99 — 7 days</option><option value="14">$199 — 14 days</option></select></label>
<button class="btn secondary small" type="submit">Request Sponsored spot</button></form></div>"""
        tapes_html = f"<h2>Your tapes</h2>{lines or '<p class=\"muted\">No tapes yet.</p>'}"
        opted = bool(user.get("cards_opt_in"))
        rips_html = f"""<h2>\U0001F0CF HITZ Rips trading cards</h2>
<div class="card"><form method="post" action="/rips/opt-in">
<label>Put me on HITZ Rips trading cards <select name="opt_in">
<option value="0" {'selected' if not opted else ''}>No — keep me off the cards</option>
<option value="1" {'selected' if opted else ''}>YES — put me on the cards</option></select></label>
<p class="muted">Opted-in artists who are charting get a trading card (you + your top tape's cover). When a fan pulls your card from a paid pack, you earn from the Artist Pool — 40% of every pack, split by card value. Card pool earnings land in your Earnings above like tips and sales.</p>
<button class="btn small" type="submit">Save card setting</button></form>
<p style="margin-top:8px"><a href="/rips">See the Artist Power Rankings →</a></p></div>"""
    req_lines = "".join(
        f"<tr><td>{html.escape(r['kind'])}</td><td>{fmt(r['amount_cents'])}</td>"
        f"<td>{html.escape(r['status'])}</td><td>{html.escape(r['created_at'][:10])}</td></tr>"
        for r in my_requests)
    body = f"""<h1>Dashboard</h1>
<p>{html.escape(user['display_name'])} · {html.escape(user['role'])}{' · <b class="gold">PRO</b>' if user['is_pro'] else ''}{' · <b class="gold">PREMIUM</b>' if user['is_premium'] else ''}</p>
<p class="muted">{money_label()}.</p>
<div class="grid">
<div class="card"><h3>Earnings — Paid</h3><p style="font-size:1.5rem" data-paid-total="{paid_total}">{fmt(paid_total)}</p>
<p class="muted">Cash-out threshold {fmt(CASHOUT_THRESHOLD_CENTS)}. Paid means money actually collected and fulfilled.</p></div>
<div class="card"><h3>Earnings — Pending</h3><p style="font-size:1.5rem" data-pending-total="{pend_total}">{fmt(pend_total)}</p>
<p class="muted">Requests waiting on payment/fulfillment. Not a balance yet.</p></div>
<div class="card"><h3>Followers</h3><p style="font-size:1.5rem" data-followers="{followers}">{followers}</p></div>
</div>
{pro_nudge}
<h2>Your profile</h2>
<div class="card"><form method="post" action="/profile">
<label>City / hometown <input name="city" value="{html.escape(user['city'] or '')}" placeholder="Chicago, IL"></label>
{'<label>Your vanity link — hitzmixtapes.com/@<input name="vanity" value="' + html.escape(user['vanity'] or '') + '" placeholder="yourname" style="max-width:220px"></label>' if user['role'] == 'artist' else ''}
<button class="btn small" type="submit">Save profile</button></form>
{f"<p class='muted'>Your link: <b>hitzmixtapes.com/@{html.escape(user['vanity'])}</b> — put it in your bio everywhere.</p>" if user['role'] == 'artist' and user['vanity'] else ''}</div>
<h2>Optional upgrades</h2>
<div style="display:flex;gap:8px;flex-wrap:wrap">
<form method="post" action="/pro-request"><button class="btn secondary" type="submit">Artist Pro — {fmt(PRO_PRICE_CENTS)}/mo (optional)</button></form>
<form method="post" action="/premium-request"><button class="btn secondary" type="submit">Fan Premium — {fmt(PREMIUM_PRICE_CENTS)}/mo (optional)</button></form>
</div>
<h2>Your requests</h2>
<table><tr><th>Type</th><th>Amount</th><th>Status</th><th>Date</th></tr>{req_lines or '<tr><td colspan="4" class="muted">None yet</td></tr>'}</table>
{rips_html}
{tapes_html}"""
    conn.close()
    return layout(request, "Dashboard", body, user)


@app.post("/tape/{tape_id}/mode")
def switch_mode(request: Request, tape_id: int, mode: str = Form(...), price: float = Form(0)):
    user, redir = require_login(request)
    if redir:
        return redir
    if mode not in ("stream_only", "free", "paid"):
        return PlainTextResponse("Invalid mode", status_code=400)
    conn = get_db()
    tape = conn.execute("SELECT * FROM mixtapes WHERE id=?", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    if tape["artist_id"] != user["id"]:
        conn.close()
        return PlainTextResponse("Only the tape's artist can change its download mode.", status_code=403)
    price_cents = int(round(price * 100)) if mode == "paid" else 0
    if mode == "paid" and price_cents < 50:
        conn.close()
        return PlainTextResponse("Paid Download price must be at least $0.50.", status_code=400)
    conn.execute("UPDATE mixtapes SET download_mode=?, price_cents=? WHERE id=?",
                 (mode, price_cents, tape_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)


# ---------------------------------------------------------------- admin API
# There is NO admin login on this site. Ken checks and runs HITZ from the
# HERE owner panel (/owner/hitz); that server calls these endpoints with the
# shared secret in the X-Admin-Key header (env HITZ_ADMIN_KEY on both apps).

def _admin_guard(request: Request):
    if not ADMIN_API_KEY:
        return JSONResponse({"ok": False, "error": "admin api not configured"}, status_code=503)
    if not is_admin(request):
        return JSONResponse({"ok": False, "error": "admin only"}, status_code=403)
    return None


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    err = _admin_guard(request)
    if err:
        return err
    conn = get_db()

    def count(sql, args=()):
        return conn.execute(sql, args).fetchone()["c"]

    pending = conn.execute(
        "SELECT t.*, p.display_name payer_name, a.display_name artist_name "
        "FROM transactions t LEFT JOIN users p ON p.id=t.payer_id "
        "LEFT JOIN users a ON a.id=t.artist_id "
        "WHERE t.status IN ('unpaid_request','waitlist') ORDER BY t.created_at").fetchall()
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) amt, COALESCE(SUM(artist_cents),0) art, "
        "COALESCE(SUM(platform_cents),0) plat FROM transactions WHERE status='paid'").fetchone()
    reports = conn.execute(
        "SELECT rp.*, m.title FROM reports rp JOIN mixtapes m ON m.id=rp.tape_id "
        "WHERE rp.status='open' ORDER BY rp.created_at").fetchall()
    tapes = conn.execute(
        "SELECT m.*, u.display_name artist_name, u.vanity artist_vanity, u.city artist_city FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 ORDER BY m.created_at DESC").fetchall()
    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 25").fetchall()
    data = {
        "ok": True,
        "app": "hitz",
        "payments_connected": PAYMENTS_CONNECTED,
        "stats": {
            "users": count("SELECT COUNT(*) c FROM users"),
            "artists": count("SELECT COUNT(*) c FROM users WHERE role='artist'"),
            "fans": count("SELECT COUNT(*) c FROM users WHERE role='fan'"),
            "tapes": count("SELECT COUNT(*) c FROM mixtapes WHERE removed=0"),
            "tracks": count("SELECT COUNT(*) c FROM tracks"),
            "plays": count("SELECT COUNT(*) c FROM events WHERE kind='play'"),
            "downloads": count("SELECT COUNT(*) c FROM events WHERE kind='download'"),
            "likes": count("SELECT COUNT(*) c FROM likes"),
            "follows": count("SELECT COUNT(*) c FROM follows"),
            "comments": count("SELECT COUNT(*) c FROM comments"),
            "open_reports": len(reports),
            "sponsored_active": len(sponsored_active(conn)),
            "sponsored_max": SPONSORED_MAX_ACTIVE,
            "paid_total_cents": paid["amt"],
            "paid_artist_cents": paid["art"],
            "paid_platform_cents": paid["plat"],
            "pending_total_cents": sum(r["amount_cents"] for r in pending),
            "pending_count": len(pending),
        },
        "pending": [{
            "id": r["id"], "kind": r["kind"], "amount_cents": r["amount_cents"],
            "artist_cents": r["artist_cents"], "platform_cents": r["platform_cents"],
            "status": r["status"], "meta": r["meta"] or "",
            "payer_name": r["payer_name"] or "", "artist_name": r["artist_name"] or "",
            "tape_id": r["tape_id"], "created_at": r["created_at"],
        } for r in pending],
        "reports": [{
            "id": r["id"], "tape_id": r["tape_id"], "title": r["title"],
            "reason": r["reason"], "created_at": r["created_at"],
        } for r in reports],
        "tapes": [{
            "id": t["id"], "title": t["title"], "artist_name": t["artist_name"],
            "download_mode": t["download_mode"], "price_cents": t["price_cents"],
            "plays": count("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='play'", (t["id"],)),
            "downloads": count("SELECT COUNT(*) c FROM events WHERE tape_id=? AND kind='download'", (t["id"],)),
            "created_at": t["created_at"],
        } for t in tapes],
        "users": [{
            "id": u["id"], "display_name": u["display_name"], "email": u["email"],
            "role": u["role"], "is_pro": bool(u["is_pro"]), "is_premium": bool(u["is_premium"]),
            "created_at": u["created_at"],
        } for u in users],
    }
    conn.close()
    return JSONResponse(data)


def _do_fulfill(conn, tx) -> str:
    """Mark a money request fulfilled (payment verified by Ken). Returns new status."""
    if tx["kind"] == "sponsored":
        if len(sponsored_active(conn)) >= SPONSORED_MAX_ACTIVE:
            conn.execute("UPDATE transactions SET status='waitlist' WHERE id=?", (tx["id"],))
            conn.commit()
            return "waitlist"
        days = 7
        for d in SPONSORED_TIERS:
            if f"sponsored_{d}d" in (tx["meta"] or ""):
                days = d
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        conn.execute("UPDATE transactions SET status='paid', fulfilled_at=?, expires_at=? WHERE id=?",
                     (now_iso(), expires, tx["id"]))
    else:
        conn.execute("UPDATE transactions SET status='paid', fulfilled_at=? WHERE id=?",
                     (now_iso(), tx["id"]))
    if tx["kind"] == "pro" and tx["payer_id"]:
        conn.execute("UPDATE users SET is_pro=1 WHERE id=?", (tx["payer_id"],))
    if tx["kind"] == "premium" and tx["payer_id"]:
        conn.execute("UPDATE users SET is_premium=1 WHERE id=?", (tx["payer_id"],))
    if tx["kind"] == "paid_download" and tx["tape_id"] and tx["payer_id"]:
        count_download_once(conn, tx["tape_id"], f"user:{tx['payer_id']}:paid")
    conn.commit()
    return "paid"


@app.post("/api/admin/fulfill/{tx_id}")
def admin_fulfill(request: Request, tx_id: int):
    err = _admin_guard(request)
    if err:
        return err
    conn = get_db()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx or tx["status"] == "paid":
        conn.close()
        return JSONResponse({"ok": False, "error": "request not found or already fulfilled"}, status_code=404)
    status = _do_fulfill(conn, tx)
    conn.close()
    return JSONResponse({"ok": True, "status": status})


@app.post("/api/admin/resolve-report/{report_id}")
def admin_resolve(request: Request, report_id: int):
    err = _admin_guard(request)
    if err:
        return err
    conn = get_db()
    conn.execute("UPDATE reports SET status='resolved' WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/admin/remove-tape/{tape_id}")
def admin_remove(request: Request, tape_id: int):
    err = _admin_guard(request)
    if err:
        return err
    conn = get_db()
    conn.execute("UPDATE mixtapes SET removed=1 WHERE id=?", (tape_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})
