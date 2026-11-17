#!/usr/bin/env python3
"""HITZ acceptance tests — run with the project venv:
   /tmp/demhits-venv/bin/python tests/run_tests.py
Uses a temp DATA_DIR/DB and generated WAV audio. Production data untouched.
"""
import io
import math
import os
import sqlite3
import struct
import sys
import tempfile
import wave
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="demhits-test-")
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
WAV2 = make_wav(660)


def signup(client, name, email, role):
    r = client.post("/signup", data={"display_name": name, "email": email,
                                     "password": "testpass1", "role": role},
                    follow_redirects=False)
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    return r


def upload(client, title, mode="free", price="10", rights=True, tracks=None, cover=PNG_COVER):
    files = [("cover", ("cover.png", cover, "image/png"))]
    for i, t in enumerate(tracks if tracks is not None else [("t1.wav", WAV1, "audio/wav")]):
        files.append(("tracks", t))
    data = {"title": title, "genre": "Hip-Hop", "description": "test tape",
            "download_mode": mode, "price": price}
    if rights:
        data["rights"] = "yes"
    return client.post("/upload", data=data, files=files, follow_redirects=False)


def tape_data_attrs(client, tape_id):
    r = client.get(f"/tape/{tape_id}")
    assert r.status_code == 200, r.status_code
    out = {}
    for k in ("plays", "downloads", "likes"):
        marker = f'data-{k}="'
        i = r.text.index(marker) + len(marker)
        out[k] = int(r.text[i:r.text.index('"', i)])
    return out


def db():
    return sqlite3.connect(str(Path(TMP) / "demhits.db"))


artist = TestClient(demhits.app)
fan = TestClient(demhits.app)
fan2 = TestClient(demhits.app)
admin = TestClient(demhits.app)
anon = TestClient(demhits.app)

# 1 healthz
r = anon.get("/healthz")
check("healthz 200", r.status_code == 200 and r.json()["ok"] is True)

# sponsored spots on empty DB
r = anon.get("/api/sponsored-spots")
check("sponsored spots 5/5 on empty DB", r.json() == {"total": 5, "active": 0, "left": 5}, str(r.json()))

# accounts
signup(artist, "Test Artist", "artist@test.local", "artist")
signup(fan, "Test Fan", "fan@test.local", "fan")
signup(fan2, "Other Fan", "fan2@test.local", "fan")

# 2 fan upload rejected
r = upload(fan, "Fan Tape")
check("fan upload rejected 403", r.status_code == 403, str(r.status_code))

# 3 upload without rights attestation rejected
r = upload(artist, "No Rights Tape", rights=False)
check("upload without rights attestation rejected 400", r.status_code == 400, str(r.status_code))

# 4 non-audio track rejected
r = upload(artist, "Video Tape", tracks=[("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32, "video/mp4")])
check("non-audio (.mp4) track rejected 400", r.status_code == 400, str(r.status_code))

# 5 valid artist tape with 2 tracks, FREE mode
r = upload(artist, "First Tape", mode="free",
           tracks=[("song-one.wav", WAV1, "audio/wav"), ("song-two.wav", WAV2, "audio/wav")])
check("valid artist tape posts (303 to tape page)", r.status_code == 303 and "/tape/" in r.headers["location"],
      f"{r.status_code} {r.text[:200]}")
tape1 = int(r.headers["location"].rstrip("/").split("/")[-1])
conn = db()
trows = conn.execute("SELECT file_path FROM tracks WHERE tape_id=? ORDER BY track_num", (tape1,)).fetchall()
check("tape has 2 tracks in DB", len(trows) == 2)
check("track files exist on disk under DATA_DIR", all(Path(p[0]).exists() for p in trows))
artist_id = conn.execute("SELECT artist_id FROM mixtapes WHERE id=?", (tape1,)).fetchone()[0]
track1_id = conn.execute("SELECT id FROM tracks WHERE tape_id=? ORDER BY track_num", (tape1,)).fetchone()[0]
conn.close()

# 6 streaming + Range
r = fan.get(f"/stream/{track1_id}")
check("stream endpoint 200", r.status_code == 200 and len(r.content) == len(WAV1))
r = fan.get(f"/stream/{track1_id}", headers={"Range": "bytes=0-99"})
check("range request 206 + correct bytes", r.status_code == 206 and r.content == WAV1[:100]
      and r.headers.get("content-range", "").startswith("bytes 0-99/"), f"{r.status_code} {r.headers.get('content-range')}")

# golden tape: nobody earns it with zero activity
r = fan.get("/")
check("no golden tape badge before any real plays", "golden-tape.jpg" not in r.text)

# 7 play dedupe
r1 = fan.post(f"/play/{tape1}")
r2 = fan.post(f"/play/{tape1}")
check("play dedupe: first counted, second not", r1.json()["counted"] is True and r2.json()["counted"] is False
      and r2.json()["plays"] == 1, f"{r1.json()} {r2.json()}")

# 8 free download counts once + auto-follows
r = fan.get(f"/download/{tape1}")
check("free download returns zip 200", r.status_code == 200 and r.headers["content-type"] == "application/zip")
r = fan.get(f"/download/{tape1}")
attrs = tape_data_attrs(fan, tape1)
check("free download counted once", attrs["downloads"] == 1, str(attrs))
conn = db()
fcount = conn.execute("SELECT COUNT(*) FROM follows WHERE follower_id=(SELECT id FROM users WHERE email='fan@test.local') AND artist_id=?",
                      (artist_id,)).fetchone()[0]
conn.close()
check("free download auto-followed the artist", fcount == 1)

# 9 follower notification on next tape
r = upload(artist, "Second Tape", mode="stream_only", tracks=[("only.wav", WAV1, "audio/wav")])
tape2 = int(r.headers["location"].rstrip("/").split("/")[-1])
r = fan.get("/notifications")
check("follower got new-drop notification", "Second Tape" in r.text)

# 10 Stream Only download rejected (even for a logged-in fan)
r = fan.get(f"/download/{tape2}")
check("stream-only download rejected 403", r.status_code == 403, str(r.status_code))

# 11 mode switch free->paid: non-owner rejected, owner works
r = fan.post(f"/tape/{tape1}/mode", data={"mode": "paid", "price": "10"})
check("non-owner mode switch rejected 403", r.status_code == 403, str(r.status_code))
r = artist.post(f"/tape/{tape1}/mode", data={"mode": "paid", "price": "10"}, follow_redirects=False)
check("owner mode switch free->paid works", r.status_code == 303, str(r.status_code))
r = fan.post(f"/play/{tape1}")  # still streamable
check("paid tape still streams/plays", r.status_code == 200)

# 12 paid download: request splits exactly, no count until fulfill, then counts
before = tape_data_attrs(fan, tape1)["downloads"]
r = fan.post(f"/buy-download/{tape1}", data={}, follow_redirects=False)
check("paid download request created", r.status_code == 303, str(r.status_code))
conn = db()
tx = conn.execute("SELECT * FROM transactions WHERE kind='paid_download' AND tape_id=? ORDER BY id DESC", (tape1,)).fetchone()
cols = [d[0] for d in conn.execute("SELECT * FROM transactions LIMIT 1").description]
txd = dict(zip(cols, tx))
conn.close()
check("$10 paid download splits $8 artist / $2 platform",
      txd["amount_cents"] == 1000 and txd["artist_cents"] == 800 and txd["platform_cents"] == 200, str(txd))
check("paid request saved as unpaid_request (no fake charge)", txd["status"] == "unpaid_request")
r = fan.get(f"/download/{tape1}")
check("paid download blocked before fulfillment 403", r.status_code == 403)
check("paid download not counted before fulfillment", tape_data_attrs(fan, tape1)["downloads"] == before)

# tip $10 -> $9/$1 pending
fan.post(f"/tip/{tape1}", data={"amount": "10"}, follow_redirects=False)
conn = db()
tip = conn.execute("SELECT artist_cents, platform_cents FROM transactions WHERE kind='tip' ORDER BY id DESC").fetchone()
conn.close()
check("$10 tip splits $9 artist / $1 platform", tip == (900, 100), str(tip))

# earnings dashboard: paid $0, pending tracked separately (800 dl + 900 tip = 1700 pending artist share)
r = artist.get("/dashboard")
check("earnings shows $0 paid with pending tracked separately",
      'data-paid-total="0"' in r.text and 'data-pending-total="1700"' in r.text)

# admin is API-only now (HERE owner panel): no login page exists on the site
r = admin.get("/admin")
check("admin login page is gone", r.status_code == 404, str(r.status_code))
r = admin.get("/api/admin/overview")
check("admin api rejects missing key", r.status_code == 403, str(r.status_code))
r = admin.get("/api/admin/overview", headers={"X-Admin-Key": "test-admin-key"})
check("admin api overview works with key", r.status_code == 200 and r.json()["ok"], str(r.status_code))
r = admin.post(f"/api/admin/fulfill/{txd['id']}", headers={"X-Admin-Key": "test-admin-key"})
check("admin fulfill paid download", r.status_code == 200 and r.json()["status"] == "paid", str(r.status_code))
after = tape_data_attrs(fan, tape1)["downloads"]
check("paid download counts after fulfillment", after == before + 1, f"{before} -> {after}")
r = fan.get(f"/download/{tape1}")
check("buyer can download after fulfillment", r.status_code == 200)
r = artist.get("/dashboard")
check("earnings paid updates to $8.00 after fulfillment", 'data-paid-total="800"' in r.text)

# golden tape: tape1 leads on real plays/downloads and wears the badge; tape2 does not
r = fan.get("/")
check("golden tape badge on homepage for the leader", "golden-tape.jpg" in r.text and "GOLDEN TAPE" in r.text)
r = fan.get(f"/tape/{tape1}")
check("golden tape pill on the winning tape page", "GOLDEN TAPE" in r.text)
r = fan.get(f"/tape/{tape2}")
check("no golden tape on the other tape page", "GOLDEN TAPE" not in r.text)
r = fan.get("/charts")
check("golden tape marks the all-time #1 on charts", "golden-tape.jpg" in r.text)

print(f"\nALL {len(PASS)} CHECKS PASSED")
