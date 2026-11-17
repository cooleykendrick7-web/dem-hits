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
ADMIN_PIN = os.environ.get("ADMIN_PIN", "0000")  # MUST be overridden by env in production
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PAYMENTS_CONNECTED = bool(STRIPE_SECRET_KEY)

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
    """)
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


def admin_token() -> str:
    return hmac.new(SESSION_SECRET.encode(), ADMIN_PIN.encode(), hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    tok = request.cookies.get("dh_admin")
    return bool(tok) and hmac.compare_digest(tok, admin_token())


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


def chart_rows(conn, since_iso=None, limit=50):
    rows = conn.execute(
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 ORDER BY m.created_at DESC").fetchall()
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


def sponsored_active(conn):
    return conn.execute(
        "SELECT t.*, m.title tape_title FROM transactions t LEFT JOIN mixtapes m ON m.id=t.tape_id "
        "WHERE t.kind='sponsored' AND t.status='paid' AND (t.expires_at IS NULL OR t.expires_at>?) "
        "ORDER BY t.fulfilled_at DESC", (now_iso(),)).fetchall()


def money_label():
    return "Payments connected" if PAYMENTS_CONNECTED else "Payments being connected — requests are saved, no charge is made yet"


def fmt(cents: int) -> str:
    return f"${cents/100:,.2f}"


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
"""

PLAYER_JS = """
let DH = {list: [], idx: 0, tapeId: null, playedTape: null};
const aud = () => document.getElementById('dh-audio');
function dhPlay(tracks, idx, tapeId){
  DH.list = tracks; DH.idx = idx; DH.tapeId = tapeId; DH.playedTape = null;
  const t = tracks[idx];
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


def layout(request: Request, title: str, body: str, user=None) -> HTMLResponse:
    if user is None:
        user = current_user(request)
    conn = get_db()
    unread = 0
    if user:
        unread = conn.execute("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                              (user["id"],)).fetchone()["c"]
    conn.close()
    if user:
        auth_links = (f'<a href="/dashboard">Dashboard</a>'
                      f'<a href="/notifications">Alerts{" (" + str(unread) + ")" if unread else ""}</a>'
                      f'<a href="/logout">Log out</a>')
        if user["role"] == "artist":
            auth_links = '<a href="/upload">Upload</a>' + auth_links
    else:
        auth_links = '<a href="/login">Log in</a><a href="/signup">Sign up free</a>'
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — HITZ</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/static/icon-192.png">
<style>{CSS}</style></head><body>
<header class="top">
<a href="/"><img class="logo" src="/static/hitz-logo.jpg" alt="HITZ — The Real Mixtape Connect"></a>
<nav class="main">
<a href="/">Discover</a><a href="/charts">Charts</a><a href="/get-paid">💰 Get Paid</a><a href="/faq">FAQ</a>
{auth_links}<a href="/admin">Admin</a>
</nav></header>
<div class="wrap">{body}</div>
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


def tape_card(conn, tape, winner_id=None) -> str:
    s = tape_stats(conn, tape["id"])
    cover = f"/cover/{tape['id']}" if tape["cover_path"] else "/static/hitz-logo.jpg"
    winner = '<span class="badge">🏆 CHART WINNER</span>' if winner_id == tape["id"] else ""
    sponsored = conn.execute(
        "SELECT 1 FROM transactions WHERE tape_id=? AND kind='sponsored' AND status='paid' "
        "AND (expires_at IS NULL OR expires_at>?)", (tape["id"], now_iso())).fetchone()
    sp = '<span class="badge silver">SPONSORED</span>' if sponsored else ""
    return f"""<div class="card">
<a href="/tape/{tape['id']}"><img class="cover" src="{cover}" alt="cover"></a>
<div style="margin-top:8px"><a href="/tape/{tape['id']}" style="font-weight:800;color:#fff">{html.escape(tape['title'])}</a></div>
<div class="muted">by <a href="/artist/{tape['artist_id']}">{html.escape(tape['artist_name'])}</a> · {html.escape(tape['genre'] or '')}</div>
<div><span class="badge dark">{mode_badge(tape)}</span>{winner}{sp}</div>
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
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.removed=0 ORDER BY m.created_at DESC LIMIT 12").fetchall()
    trending = chart_rows(conn, limit=6)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=1)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None
    sponsored = sponsored_active(conn)
    sp_ids = [r["tape_id"] for r in sponsored]
    cards = "".join(tape_card(conn, dict(t), winner_id) for t in latest)
    trend_cards = "".join(tape_card(conn, t, winner_id) for _, t, _ in trending if t["id"] not in [x["id"] for x in latest])
    sp_cards = ""
    if sp_ids:
        rows = conn.execute(
            "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id "
            f"WHERE m.id IN ({','.join('?' * len(sp_ids))})", sp_ids).fetchall()
        sp_cards = ("<h2>Sponsored</h2><div class='grid'>" +
                    "".join(tape_card(conn, dict(r), winner_id) for r in rows) + "</div>")
    conn.close()
    if not latest:
        empty = """<div class="card" style="text-align:center;padding:28px">
<h3>Be The First</h3><p class="muted">No tapes yet. The first artist to drop sets the tone.</p>
<a class="btn" href="/signup">Create a free Artist account</a></div>"""
        cards = empty
    body = f"""
<div class="hero"><img src="/static/hitz-logo.jpg" alt="HITZ logo">
<h1>HITZ — <span class="silver">THE REAL MIXTAPE CONNECT</span></h1>
<div class="bigfree">FREE TO UPLOAD</div>
<p style="font-size:1.1rem">Artists post mixtapes free, fans stream free — and artists actually <b class="gold">get paid</b>:
80% of sales, 90% of tips, 80% of fan subs.</p>
<p><a class="btn" href="/get-paid">💰 See how you get paid</a> <a class="btn secondary" href="/signup">Create Free Artist Account</a></p>
</div>
{sp_cards}
<h2>Latest Drops</h2><div class="grid">{cards}</div>
<h2 style="margin-top:28px">Trending</h2><div class="grid">{trend_cards or '<p class="muted">Nothing trending yet.</p>'}</div>
{SHARE_JS}"""
    return layout(request, "Discover", body, user)


@app.get("/charts")
def charts(request: Request):
    conn = get_db()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = chart_rows(conn, since_iso=week_ago, limit=20)
    alltime = chart_rows(conn, limit=20)
    winner_id = weekly[0][1]["id"] if weekly and weekly[0][0] > 0 else None

    def rows(data):
        if not data or all(s == 0 for s, _, _ in data):
            return "<p class='muted'>No chart activity yet — be the first.</p>"
        trs = ""
        for i, (score, t, s) in enumerate(data, 1):
            trs += (f"<tr><td>{i}</td><td><a href='/tape/{t['id']}'>{html.escape(t['title'])}</a>"
                    f"{' 🏆' if t['id'] == winner_id else ''}</td>"
                    f"<td><a href='/artist/{t['artist_id']}'>{html.escape(t['artist_name'])}</a></td>"
                    f"<td>{mode_badge(t)}</td><td>{s['plays']}</td><td>{s['downloads']}</td><td>{s['likes']}</td></tr>")
        return ("<table><tr><th>#</th><th>Tape</th><th>Artist</th><th>Mode</th>"
                "<th>Plays</th><th>Downloads</th><th>Likes</th></tr>" + trs + "</table>")

    body = f"<h1>Charts</h1><h2>Top This Week</h2>{rows(weekly)}<h2 style='margin-top:30px'>Top All-Time</h2>{rows(alltime)}{SHARE_JS}"
    conn.close()
    return layout(request, "Charts", body)


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


# ---------------------------------------------------------------- auth routes
@app.get("/signup")
def signup_page(request: Request):
    body = """<h1>Sign up free</h1>
<p><span class="bigfree">FREE TO UPLOAD</span></p>
<form method="post" action="/signup">
<label>Display name <input name="display_name" required></label>
<label>Email <input name="email" type="email" required></label>
<label>Password <input name="password" type="password" required minlength="6"></label>
<label>I am an <select name="role"><option value="artist">Artist — I post mixtapes</option>
<option value="fan">Fan — I listen, download, tip</option></select></label>
<button class="btn" type="submit">Create free account</button></form>
<p class="muted">Already have an account? <a href="/login">Log in</a></p>"""
    return layout(request, "Sign up", body)


@app.post("/signup")
def signup(request: Request, display_name: str = Form(...), email: str = Form(...),
           password: str = Form(...), role: str = Form(...)):
    if role not in ("artist", "fan"):
        return PlainTextResponse("Invalid role", status_code=400)
    email = email.strip().lower()
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return PlainTextResponse("An account with that email already exists", status_code=400)
    pw_hash, pw_salt = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users(email,display_name,role,pw_hash,pw_salt,created_at) VALUES(?,?,?,?,?,?)",
        (email, display_name.strip(), role, pw_hash, pw_salt, now_iso()))
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


# ---------------------------------------------------------------- upload
@app.get("/upload")
def upload_page(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    if user["role"] != "artist":
        return PlainTextResponse("Only Artist accounts can upload mixtapes.", status_code=403)
    body = """<h1>Post a mixtape</h1>
<p><span class="bigfree">FREE TO UPLOAD</span></p>
<p class="muted">No card, no fee. Pro and Sponsored are optional — never required to post or earn.</p>
<form method="post" action="/upload" enctype="multipart/form-data">
<label>Mixtape title <input name="title" required></label>
<label>Cover image (jpg / png / webp) <input name="cover" type="file" accept=".jpg,.jpeg,.png,.webp" required></label>
<label>Genre <input name="genre" placeholder="Hip-Hop / Rap"></label>
<label>Description <textarea name="description" rows="3"></textarea></label>
<label>Download mode
<select name="download_mode">
<option value="free">FREE DOWNLOAD — fans with a free account can download (builds your fan list)</option>
<option value="stream_only">STREAM ONLY — listen on the site only, no downloads for anyone</option>
<option value="paid">PAID DOWNLOAD — stream free, download costs your price</option>
</select></label>
<label>Price if Paid Download ($) <input name="price" type="number" step="0.5" min="1" value="10"></label>
<label>Audio tracks — mp3 / wav / m4a / ogg, up to 40MB each, add at least 1
<input name="tracks" type="file" accept=".mp3,.wav,.m4a,.ogg" multiple required></label>
<label style="display:flex;gap:8px;align-items:flex-start">
<input type="checkbox" name="rights" value="yes" style="width:auto;margin-top:3px">
<span>I confirm this music is mine or I have permission to post it. I keep 100% of my rights.</span></label>
<button class="btn" type="submit">Post my mixtape — free</button></form>"""
    return layout(request, "Upload", body, user)


@app.post("/upload")
async def upload(request: Request, title: str = Form(...), genre: str = Form(""),
                 description: str = Form(""), download_mode: str = Form("free"),
                 price: float = Form(0), rights: str = Form(None),
                 cover: UploadFile = File(...), tracks: list[UploadFile] = File(...)):
    user = current_user(request)
    if not user:
        return PlainTextResponse("Log in first", status_code=401)
    if user["role"] != "artist":
        return PlainTextResponse("Only Artist accounts can upload mixtapes.", status_code=403)
    if rights != "yes":
        return PlainTextResponse("You must confirm you own this music or have permission to post it (rights attestation required).", status_code=400)
    if download_mode not in ("stream_only", "free", "paid"):
        return PlainTextResponse("Invalid download mode", status_code=400)
    if not tracks:
        return PlainTextResponse("Add at least one audio track.", status_code=400)
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

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO mixtapes(artist_id,title,cover_path,genre,description,download_mode,price_cents,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user["id"], title.strip(), "", genre.strip(), description.strip(),
         download_mode, price_cents, now_iso()))
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
                     (f["follower_id"], f"{user['display_name']} just dropped a new tape: {title.strip()}", tape_id, now_iso()))
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
    user = current_user(request)
    row = conn.execute(
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.id=?",
        (tape_id,)).fetchone()
    if not row or row["removed"]:
        conn.close()
        return PlainTextResponse("Mixtape not found", status_code=404)
    tape = dict(row)
    tracks = conn.execute("SELECT * FROM tracks WHERE tape_id=? ORDER BY track_num", (tape_id,)).fetchall()
    s = tape_stats(conn, tape_id)
    followers = conn.execute("SELECT COUNT(*) c FROM follows WHERE artist_id=?", (tape["artist_id"],)).fetchone()["c"]
    js_tracks = json.dumps([{"id": t["id"], "title": t["title"], "artist": tape["artist_name"]} for t in tracks])
    track_lines = "".join(
        f"""<div class="trackline"><button class="btn small" onclick='dhPlay({js_tracks},{i},{tape_id})'>▶</button>
<span>{i+1}. {html.escape(t['title'])}</span></div>""" for i, t in enumerate(tracks))
    if tape["download_mode"] == "stream_only":
        dl = '<p><span class="badge dark">STREAM ONLY</span> <span class="muted">Downloads are off for this tape — the artist\'s setting always wins.</span></p>'
    elif tape["download_mode"] == "free":
        dl = (f'<p><a class="btn" href="/download/{tape_id}">⬇ Free Download</a> '
              '<span class="muted">Free Fan account required — downloading follows this artist.</span></p>')
    else:
        dl = (f'<form method="post" action="/buy-download/{tape_id}" style="display:inline">'
              f'<button class="btn" type="submit">⬇ Buy Download — {fmt(tape["price_cents"])}</button></form> '
              f'<a class="btn secondary" href="/download/{tape_id}">I already paid — download</a>'
              f'<p class="muted">You keep streaming free. The artist keeps 80% (85% on Pro). {money_label()}.</p>')
    body = f"""
<img src="/cover/{tape_id}" alt="cover" style="width:220px;height:220px;object-fit:cover;border-radius:12px">
<h1>{html.escape(tape['title'])}</h1>
<p>by <a href="/artist/{tape['artist_id']}">{html.escape(tape['artist_name'])}</a> · {html.escape(tape['genre'] or '')} · {followers} followers</p>
<p><span class="badge dark">{mode_badge(tape)}</span></p>
<p class="muted" data-plays="{s['plays']}" data-downloads="{s['downloads']}" data-likes="{s['likes']}">
▶ {s['plays']} plays · ⬇ {s['downloads']} downloads · ♥ {s['likes']} likes</p>
<p>{html.escape(tape['description'] or '')}</p>
{track_lines}
<div style="margin:16px 0">{dl}</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:14px 0">
<form method="post" action="/like/{tape_id}"><button class="btn secondary small" type="submit">♥ Like</button></form>
<form method="post" action="/follow/{tape['artist_id']}"><button class="btn secondary small" type="submit">Follow Artist</button></form>
<button class="btn secondary small" onclick="dhShare('{html.escape(tape['artist_name'], quote=True)}','{html.escape(tape['title'], quote=True)}')">Share</button>
</div>
<h3>Tip the artist — they keep 90%</h3>
<form method="post" action="/tip/{tape_id}">
<label>Tip amount ($) <input name="amount" type="number" step="0.5" min="0.5" value="5" style="max-width:160px"></label>
<button class="btn small" type="submit">Tip $1 / $5 / $10 — any amount</button>
<span class="muted">{money_label()}.</span></form>
<h3 style="margin-top:22px">Report</h3>
<form method="post" action="/report/{tape_id}">
<label>Reason (copyright / DMCA / other) <input name="reason" required></label>
<button class="btn secondary small" type="submit">Report this tape</button></form>
{SHARE_JS}"""
    conn.close()
    return layout(request, tape["title"], body, user)


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
    tapes = conn.execute(
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id "
        "WHERE m.artist_id=? AND m.removed=0 ORDER BY m.created_at DESC", (artist_id,)).fetchall()
    followers = conn.execute("SELECT COUNT(*) c FROM follows WHERE artist_id=?", (artist_id,)).fetchone()["c"]
    cards = "".join(tape_card(conn, dict(t), winner_id) for t in tapes) or "<p class='muted'>No tapes yet.</p>"
    pro = ' <span class="badge">PRO ✓ VERIFIED</span>' if a["is_pro"] else ""
    body = f"""<h1>{html.escape(a['display_name'])}{pro}</h1>
<p class="muted">{followers} followers</p>
<form method="post" action="/follow/{artist_id}"><button class="btn small" type="submit">Follow Artist</button></form>
<form method="post" action="/subscribe/{artist_id}" style="margin-top:8px">
<button class="btn secondary small" type="submit">Subscribe — {fmt(SUB_PRICE_CENTS)}/mo (artist keeps 80%)</button></form>
<h2>Tapes</h2><div class="grid">{cards}</div>{SHARE_JS}"""
    conn.close()
    return layout(request, a["display_name"], body)


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
        "SELECT t.*, m.removed FROM tracks t JOIN mixtapes m ON m.id=t.tape_id WHERE t.id=?",
        (track_id,)).fetchone()
    conn.close()
    if not row or row["removed"]:
        return PlainTextResponse("Track not found", status_code=404)
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
    tape = conn.execute("SELECT id FROM mixtapes WHERE id=? AND removed=0", (tape_id,)).fetchone()
    if not tape:
        conn.close()
        return JSONResponse({"counted": False}, status_code=404)
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


# ---------------------------------------------------------------- artist dashboard
@app.get("/dashboard")
def dashboard(request: Request):
    user, redir = require_login(request)
    if redir:
        return redir
    conn = get_db()
    earn_kinds = ("tip", "paid_download", "subscription")
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
<form method="post" action="/sponsored/{t['id']}" style="margin-top:6px">
<label>Sponsor this tape <select name="days"><option value="3">$49 — 3 days</option>
<option value="7">$99 — 7 days</option><option value="14">$199 — 14 days</option></select></label>
<button class="btn secondary small" type="submit">Request Sponsored spot</button></form></div>"""
        tapes_html = f"<h2>Your tapes</h2>{lines or '<p class=\"muted\">No tapes yet.</p>'}"
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
<h2>Optional upgrades</h2>
<div style="display:flex;gap:8px;flex-wrap:wrap">
<form method="post" action="/pro-request"><button class="btn secondary" type="submit">Artist Pro — {fmt(PRO_PRICE_CENTS)}/mo (optional)</button></form>
<form method="post" action="/premium-request"><button class="btn secondary" type="submit">Fan Premium — {fmt(PREMIUM_PRICE_CENTS)}/mo (optional)</button></form>
</div>
<h2>Your requests</h2>
<table><tr><th>Type</th><th>Amount</th><th>Status</th><th>Date</th></tr>{req_lines or '<tr><td colspan="4" class="muted">None yet</td></tr>'}</table>
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


# ---------------------------------------------------------------- admin
@app.get("/admin")
def admin_page(request: Request):
    if not is_admin(request):
        body = """<h1>Admin</h1>
<form method="post" action="/admin/login">
<label>Admin PIN <input name="pin" type="password" required></label>
<button class="btn" type="submit">Enter</button></form>"""
        return layout(request, "Admin", body)
    conn = get_db()
    pending = conn.execute(
        "SELECT * FROM transactions WHERE status IN ('unpaid_request','waitlist') ORDER BY created_at").fetchall()
    pend_total = sum(r["amount_cents"] for r in pending)
    pend_lines = "".join(
        f"""<tr><td data-tx-id="{r['id']}">{r['id']}</td><td>{html.escape(r['kind'])}</td>
<td>{fmt(r['amount_cents'])}</td><td>{fmt(r['artist_cents'])}</td><td>{fmt(r['platform_cents'])}</td>
<td>{html.escape(r['status'])}</td><td>{html.escape(r['meta'] or '')}</td>
<td><form method="post" action="/admin/fulfill/{r['id']}"><button class="btn small" type="submit">Fulfill (payment verified)</button></form></td></tr>"""
        for r in pending)
    reports = conn.execute(
        "SELECT rp.*, m.title FROM reports rp JOIN mixtapes m ON m.id=rp.tape_id WHERE rp.status='open' ORDER BY rp.created_at").fetchall()
    rep_lines = "".join(
        f"""<tr><td>{r['id']}</td><td><a href="/tape/{r['tape_id']}">{html.escape(r['title'])}</a></td>
<td>{html.escape(r['reason'])}</td>
<td><form method="post" action="/admin/resolve-report/{r['id']}" style="display:inline"><button class="btn small secondary" type="submit">Resolve</button></form>
<form method="post" action="/admin/remove-tape/{r['tape_id']}" style="display:inline"><button class="btn small" type="submit">Remove tape</button></form></td></tr>"""
        for r in reports)
    tapes = conn.execute(
        "SELECT m.*, u.display_name artist_name FROM mixtapes m JOIN users u ON u.id=m.artist_id WHERE m.removed=0 ORDER BY m.created_at DESC").fetchall()
    tape_lines = "".join(
        f"""<tr><td><a href="/tape/{t['id']}">{html.escape(t['title'])}</a></td><td>{html.escape(t['artist_name'])}</td>
<td>{mode_badge(t)}</td><td><form method="post" action="/admin/remove-tape/{t['id']}"><button class="btn small secondary" type="submit">Remove</button></form></td></tr>"""
        for t in tapes)
    spots = len(sponsored_active(conn))
    body = f"""<h1>Admin — Owner</h1>
<h2>Pending Money — <span data-pending-money="{pend_total}">{fmt(pend_total)}</span> waiting</h2>
<p class="muted">Fulfill a request only after payment is verified. Fulfilling a paid download is what makes it count and downloadable.
Sponsored spots active: {spots} of {SPONSORED_MAX_ACTIVE}.</p>
<table><tr><th>ID</th><th>Type</th><th>Amount</th><th>Artist</th><th>Platform</th><th>Status</th><th>Detail</th><th></th></tr>
{pend_lines or '<tr><td colspan="8" class="muted">No pending requests</td></tr>'}</table>
<h2 style="margin-top:26px">Open Reports / DMCA</h2>
<table><tr><th>ID</th><th>Tape</th><th>Reason</th><th></th></tr>{rep_lines or '<tr><td colspan="4" class="muted">No open reports</td></tr>'}</table>
<h2 style="margin-top:26px">All Tapes</h2>
<table><tr><th>Tape</th><th>Artist</th><th>Mode</th><th></th></tr>{tape_lines}</table>"""
    conn.close()
    return layout(request, "Admin", body)


@app.post("/admin/login")
def admin_login(request: Request, pin: str = Form(...)):
    if not hmac.compare_digest(pin, ADMIN_PIN):
        return PlainTextResponse("Wrong PIN", status_code=401)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("dh_admin", admin_token(), httponly=True, samesite="lax")
    return resp


@app.post("/admin/fulfill/{tx_id}")
def admin_fulfill(request: Request, tx_id: int):
    if not is_admin(request):
        return PlainTextResponse("Admin only", status_code=403)
    conn = get_db()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx or tx["status"] == "paid":
        conn.close()
        return PlainTextResponse("Request not found or already fulfilled", status_code=404)
    msg = ""
    if tx["kind"] == "sponsored":
        if len(sponsored_active(conn)) >= SPONSORED_MAX_ACTIVE:
            conn.execute("UPDATE transactions SET status='waitlist' WHERE id=?", (tx_id,))
            conn.commit()
            conn.close()
            return RedirectResponse("/admin", status_code=303)
        days = 7
        for d in SPONSORED_TIERS:
            if f"sponsored_{d}d" in (tx["meta"] or ""):
                days = d
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        conn.execute("UPDATE transactions SET status='paid', fulfilled_at=?, expires_at=? WHERE id=?",
                     (now_iso(), expires, tx_id))
    else:
        conn.execute("UPDATE transactions SET status='paid', fulfilled_at=? WHERE id=?",
                     (now_iso(), tx_id))
    if tx["kind"] == "pro" and tx["payer_id"]:
        conn.execute("UPDATE users SET is_pro=1 WHERE id=?", (tx["payer_id"],))
    if tx["kind"] == "premium" and tx["payer_id"]:
        conn.execute("UPDATE users SET is_premium=1 WHERE id=?", (tx["payer_id"],))
    if tx["kind"] == "paid_download" and tx["tape_id"] and tx["payer_id"]:
        count_download_once(conn, tx["tape_id"], f"user:{tx['payer_id']}:paid")
    conn.commit()
    conn.close()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/resolve-report/{report_id}")
def admin_resolve(request: Request, report_id: int):
    if not is_admin(request):
        return PlainTextResponse("Admin only", status_code=403)
    conn = get_db()
    conn.execute("UPDATE reports SET status='resolved' WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/remove-tape/{tape_id}")
def admin_remove(request: Request, tape_id: int):
    if not is_admin(request):
        return PlainTextResponse("Admin only", status_code=403)
    conn = get_db()
    conn.execute("UPDATE mixtapes SET removed=1 WHERE id=?", (tape_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin", status_code=303)
