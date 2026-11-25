#!/usr/bin/env python3
"""HITZ RIPS acceptance tests — run with the project venv:
   .venv/bin/python tests/test_rips.py
Uses a temp DATA_DIR/DB and generated WAV audio. Production data untouched.
Covers: opt-in, eligibility, rankings/tiers, odds, buy gating (unpaid can't
rip), sample pack rules, paid rip, Artist Pool math (sums to exactly 400c,
proportional to card value), rip-once idempotency, sell-back rules, and
card_pool earnings landing on the artist dashboard.
"""
import io
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
import wave
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="demhits-rips-test-")
os.environ["DATA_DIR"] = TMP
os.environ["HITZ_ADMIN_KEY"] = "test-admin-key"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ.pop("STRIPE_SECRET_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402
import app as demhits  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"FAIL: {name} {extra}")
    PASS.append(name)
    print(f"  ok - {name}")


def make_wav(freq=440, seconds=0.2, rate=22050):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(rate * seconds)))
        w.writeframes(frames)
    return buf.getvalue()


PNG_COVER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WAV1 = make_wav(440)


def db():
    conn = sqlite3.connect(str(Path(TMP) / "demhits.db"))
    conn.row_factory = sqlite3.Row
    return conn


def signup(name, email, role):
    c = TestClient(demhits.app)
    r = c.post("/signup", data={"display_name": name, "email": email,
                                "password": "testpass1", "role": role},
               follow_redirects=False)
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    return c


def upload(client, title):
    files = [("cover", ("cover.png", PNG_COVER, "image/png")),
             ("tracks", ("t1.wav", WAV1, "audio/wav"))]
    data = {"title": title, "genre": "Hip-Hop", "description": "test tape",
            "download_mode": "free", "price": "10", "rights": "yes"}
    r = client.post("/upload", data=data, files=files, follow_redirects=False)
    assert r.status_code == 303, (title, r.status_code, r.text[:200])
    return int(r.headers["location"].rstrip("/").split("/")[-1])


def add_events(tape_id, kind, n, tag):
    conn = db()
    for i in range(n):
        conn.execute(
            "INSERT INTO events(tape_id,kind,listener_key,day,created_at) VALUES(?,?,?, '2026-11-20', '2026-11-20T00:00:00+00:00')",
            (tape_id, kind, f"{tag}-{kind}-{i}"))
    conn.commit()
    conn.close()


# ---- build 8 artists with distinct real scores -----------------------------
# scores (plays + 2*downloads + 3*likes): A1 100, A2 80, A3 60, A4 40, A5 20, A6 5
# A7 has the biggest score (200) but never opts in -> must NOT be eligible.
# A8 opts in but has zero activity -> must NOT be eligible.
PLAN = [("A1", 100, 0, 0), ("A2", 80, 0, 0), ("A3", 54, 3, 0), ("A4", 31, 3, 1),
        ("A5", 14, 3, 0), ("A6", 5, 0, 0), ("A7", 200, 0, 0), ("A8", 0, 0, 0)]
clients, tapes, artist_ids = {}, {}, {}
liker = signup("Liker Fan", "liker@test.local", "fan")
conn = db()
liker_id = conn.execute("SELECT id FROM users WHERE email='liker@test.local'").fetchone()["id"]
conn.close()
for name, plays, dls, likes in PLAN:
    c = signup(f"Artist {name}", f"{name.lower()}@test.local", "artist")
    t = upload(c, f"Tape {name}")
    clients[name], tapes[name] = c, t
    conn = db()
    artist_ids[name] = conn.execute("SELECT artist_id FROM mixtapes WHERE id=?", (t,)).fetchone()["artist_id"]
    conn.close()
    add_events(t, "play", plays, name)
    add_events(t, "download", dls, name)
    if likes:
        conn = db()
        conn.execute("INSERT INTO likes(user_id,tape_id,created_at) VALUES(?,?, '2026-11-20T00:00:00+00:00')",
                     (liker_id, t))
        conn.commit()
        conn.close()

fan = signup("Buyer Fan", "buyer@test.local", "fan")
fan2 = signup("Sampler Fan", "sampler@test.local", "fan")
anon = TestClient(demhits.app)
admin = TestClient(demhits.app)
AH = {"X-Admin-Key": "test-admin-key"}

# ---- pool is empty before any opt-in --------------------------------------
r = anon.get("/rips")
check("rips page 200 with honest empty state before opt-ins",
      r.status_code == 200 and "hasn't dropped yet" in r.text)
check("nav has Rips link", 'href="/rips"' in r.text)
r = fan.post("/rips/buy", follow_redirects=False)
check("buy blocked while pool empty (400)", r.status_code == 400, str(r.status_code))
r = fan.post("/rips/sample", follow_redirects=False)
check("sample blocked while pool empty (400)", r.status_code == 400, str(r.status_code))
r = anon.get("/rips/my", follow_redirects=False)
check("logged-out /rips/my redirects to login",
      r.status_code == 303 and "/login" in r.headers["location"])

# ---- opt-ins ----------------------------------------------------------------
for name in ("A1", "A2", "A3", "A4", "A5", "A6", "A8"):
    r = clients[name].post("/rips/opt-in", data={"opt_in": "1"}, follow_redirects=False)
    assert r.status_code == 303, (name, r.status_code)
r = fan.post("/rips/opt-in", data={"opt_in": "1"}, follow_redirects=False)
check("fan cannot opt in (403)", r.status_code == 403, str(r.status_code))
conn = db()
check("cards_opt_in saved", conn.execute(
    "SELECT COUNT(*) c FROM users WHERE cards_opt_in=1").fetchone()["c"] == 7)
conn.close()

# ---- rankings + tiers ---------------------------------------------------------
r = anon.get("/rips")
check("rankings show all 6 eligible artists in score order",
      all(f"Artist A{i}" in r.text for i in range(1, 7))
      and r.text.index("Artist A1") < r.text.index("Artist A2") < r.text.index("Artist A3")
      < r.text.index("Artist A4") < r.text.index("Artist A5") < r.text.index("Artist A6"))
check("non-opted-in A7 absent from rankings", "Artist A7" not in r.text)
check("zero-score opted-in A8 absent from rankings", "Artist A8" not in r.text)
check("tiers on page: Legend + Superstar + Star",
      "LEGEND" in r.text and "SUPERSTAR" in r.text and "STAR" in r.text)
check("money split posted (40/30/20/10)",
      "40% Artist Pool" in r.text and "30% HITZ" in r.text
      and "20% Printing + shipping fund" in r.text and "10% Chase fund" in r.text)
check("live odds posted", "per card" in r.text and "%" in r.text)

elig = None
conn = db()
import importlib
c2 = demhits.get_db()
elig = demhits.rips_eligible(c2)
c2.close()
check("eligible pool = 6 artists", len(elig) == 6, str(len(elig)))
check("rank order A1..A6", [e["artist_name"] for e in elig] ==
      [f"Artist A{i}" for i in range(1, 7)], str([e["artist_name"] for e in elig]))
check("tier assignment by rank",
      [e["tier"] for e in elig] == ["Legend", "Superstar", "Superstar", "Star", "Star", "Star"],
      str([e["tier"] for e in elig]))
check("tier values", [e["value_cents"] for e in elig] == [7500, 2000, 2000, 800, 800, 800])
check("artist scores are the real chart sums",
      [e["score"] for e in elig] == [100, 80, 60, 40, 20, 5], str([e["score"] for e in elig]))
conn.close()

# ---- pool share math unit check ----------------------------------------------
shares = demhits.rips_pool_shares([{"value_cents": v} for v in (7500, 100, 100, 300, 2000)])
check("pool shares sum to exactly 400", sum(shares) == 400, str(shares))
check("pool shares proportional to value",
      shares[0] > shares[4] > shares[3] > shares[1] and shares[1] == shares[2], str(shares))

# ---- sample pack ---------------------------------------------------------------
r = fan2.post("/rips/sample", follow_redirects=False)
check("sample rip redirects to reveal", r.status_code == 303 and "/rips/reveal/" in r.headers["location"],
      f"{r.status_code} {r.headers.get('location')}")
sample_txn = int(r.headers["location"].rstrip("/").split("/")[-1])
r = fan2.get(f"/rips/reveal/{sample_txn}")
check("sample reveal labels SAMPLE + $0", "SAMPLE" in r.text and "free SAMPLE pack" in r.text)
conn = db()
scards = conn.execute("SELECT * FROM user_cards WHERE txn_id=?", (sample_txn,)).fetchall()
check("sample rip drew exactly 5 cards", len(scards) == 5, str(len(scards)))
check("sample cards flagged sample + value 0",
      all(c["is_sample"] == 1 and c["value_cents"] == 0 for c in scards))
check("sample rip created NO card_pool rows",
      conn.execute("SELECT COUNT(*) c FROM transactions WHERE kind='card_pool' AND payer_id=?",
                   (scards[0]["user_id"],)).fetchone()["c"] == 0)
conn.close()
r = fan2.post("/rips/sample", follow_redirects=False)
check("second sample pack refused (400)", r.status_code == 400, str(r.status_code))
r = fan2.post(f"/rips/sellback/{scards[0]['id']}", follow_redirects=False)
check("sample card sell-back refused (400)", r.status_code == 400, str(r.status_code))

# ---- paid pack: buy gates on payment -------------------------------------------
r = fan.post("/rips/buy", follow_redirects=False)
check("buy creates the pack request (303)", r.status_code == 303, str(r.status_code))
conn = db()
pack = conn.execute("SELECT * FROM transactions WHERE kind='card_pack' ORDER BY id DESC").fetchone()
conn.close()
check("pack tx is $10 unpaid_request (payments being connected honesty)",
      pack["amount_cents"] == 1000 and pack["status"] == "unpaid_request", str(dict(pack)))
r = fan.post(f"/rips/rip/{pack['id']}", follow_redirects=False)
check("unpaid pack cannot be ripped (400)", r.status_code == 400, str(r.status_code))
r = fan2.post(f"/rips/rip/{pack['id']}", follow_redirects=False)
check("someone else's pack cannot be ripped (403)", r.status_code == 403, str(r.status_code))
r = admin.post(f"/api/admin/fulfill/{pack['id']}", headers=AH)
check("admin fulfill marks pack paid", r.status_code == 200 and r.json()["status"] == "paid")

r = fan.post(f"/rips/rip/{pack['id']}", follow_redirects=False)
check("paid pack rips (303 to reveal)", r.status_code == 303
      and f"/rips/reveal/{pack['id']}" in r.headers["location"], str(r.status_code))
r = fan.get(f"/rips/reveal/{pack['id']}")
check("reveal shows 5 cards + pool note", r.text.count('class="cover"') == 5 and "Artist Pool" in r.text)

conn = db()
cards = conn.execute("SELECT * FROM user_cards WHERE txn_id=? ORDER BY id", (pack["id"],)).fetchall()
check("paid rip persisted exactly 5 cards", len(cards) == 5, str(len(cards)))
check("paid cards are not samples and carry tier values",
      all(c["is_sample"] == 0 and c["value_cents"] == demhits.RIPS_TIER_VALUES[c["tier"]] for c in cards))
pool = conn.execute("SELECT * FROM transactions WHERE kind='card_pool' AND payer_id=?",
                    (cards[0]["user_id"],)).fetchall()
check("one card_pool row per pulled card", len(pool) == 5, str(len(pool)))
check("card_pool sums to EXACTLY 400c", sum(p["amount_cents"] for p in pool) == 400,
      str([p["amount_cents"] for p in pool]))
check("card_pool rows are paid, artist_cents == amount",
      all(p["status"] == "paid" and p["artist_cents"] == p["amount_cents"] for p in pool))
check("card_pool meta points at the pack txn",
      all(json.loads(p["meta"])["pack_txn"] == pack["id"] for p in pool))
# proportionality: recompute expected shares from the pulled values
expected = demhits.rips_pool_shares([{"value_cents": c["value_cents"]} for c in cards])
got_by_card = []
for c in cards:
    s = sum(p["amount_cents"] for p in pool
            if p["artist_id"] == c["artist_id"] and p["tape_id"] == c["tape_id"]
            and json.loads(p["meta"])["tier"] == c["tier"])
    got_by_card.append(s)
check("per-card pool shares match value-weighted split",
      sorted(got_by_card) == sorted(expected), f"got {got_by_card} expected {expected}")
conn.close()

# ---- rip-once idempotency -------------------------------------------------------
r = fan.post(f"/rips/rip/{pack['id']}", follow_redirects=False)
check("re-rip does not error out the user (303 to reveal)",
      r.status_code == 303, str(r.status_code))
conn = db()
check("re-rip created no new cards",
      conn.execute("SELECT COUNT(*) c FROM user_cards WHERE txn_id=?", (pack["id"],)).fetchone()["c"] == 5)
check("re-rip paid no new pool money",
      conn.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE kind='card_pool' AND payer_id=?",
                   (cards[0]["user_id"],)).fetchone()["s"] == 400)
conn.close()

# ---- artist earnings dashboard counts card_pool ----------------------------------
conn = db()
per_artist = {}
for p in pool:
    per_artist[p["artist_id"]] = per_artist.get(p["artist_id"], 0) + p["amount_cents"]
conn.close()
some_artist = max(per_artist, key=per_artist.get)
name_by_id = {v: k for k, v in artist_ids.items()}
r = clients[name_by_id[some_artist]].get("/dashboard")
check("card_pool earnings show in artist dashboard Paid total",
      f'data-paid-total="{per_artist[some_artist]}"' in r.text,
      f"expected {per_artist[some_artist]}")
r = anon.get("/wall-of-paid")
check("card_pool payouts appear on Wall of Paid", "Trading card pack pool" in r.text)

# ---- sell-back ---------------------------------------------------------------------
target = cards[0]
r = fan2.post(f"/rips/sellback/{target['id']}", follow_redirects=False)
check("cannot sell back someone else's card (404)", r.status_code == 404, str(r.status_code))
r = fan.post(f"/rips/sellback/{target['id']}", follow_redirects=False)
check("sell-back works (303)", r.status_code == 303, str(r.status_code))
conn = db()
c = conn.execute("SELECT sold_back FROM user_cards WHERE id=?", (target["id"],)).fetchone()
credit = conn.execute("SELECT * FROM transactions WHERE kind='card_sellback_credit'").fetchone()
check("card marked sold_back", c["sold_back"] == 1)
check("sell-back credit = card value, status credit, labeled platform credit",
      credit["amount_cents"] == target["value_cents"] and credit["status"] == "credit"
      and "platform_credit_not_cash" in credit["meta"], str(dict(credit)))
conn.close()
r = fan.post(f"/rips/sellback/{target['id']}", follow_redirects=False)
check("double sell-back refused (400)", r.status_code == 400, str(r.status_code))
r = fan.get("/rips/my")
check("collection page shows credit + sold card",
      "Platform credit" in r.text and "SOLD BACK" in r.text)

# ---- opt-out removes the artist ------------------------------------------------------
r = clients["A6"].post("/rips/opt-in", data={"opt_in": "0"}, follow_redirects=False)
check("opt-out saves", r.status_code == 303, str(r.status_code))
r = anon.get("/rips")
check("opted-out artist leaves the rankings", "Artist A6" not in r.text)

print(f"\nALL {len(PASS)} RIPS CHECKS PASSED")
