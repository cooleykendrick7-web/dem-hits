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
check("no golden tape on the other tape page", "GOLDEN TAPE — most plays on HITZ" not in r.text)
r = fan.get("/charts")
check("golden tape marks the all-time #1 on charts", "golden-tape.jpg" in r.text)

# ---------------- growth pack
from datetime import datetime, timedelta, timezone

r = fan.get(f"/tape/{tape1}")
check("tape page has rich share preview (og tags)", 'property="og:title"' in r.text and f"/cover/{tape1}" in r.text and "summary_large_image" in r.text)
check("tape page has MusicAlbum structured data", "MusicAlbum" in r.text)
r = anon.get(f"/embed/{tape1}")
check("embed player loads with tracks + signup hook", r.status_code == 200 and "/stream/" in r.text and "Drop your tape on HITZ" in r.text)
r = fan.get("/@test_artist")
check("vanity link opens the artist page", r.status_code == 200 and "Test Artist" in r.text)
r = fan.get("/genre/hip-hop")
check("genre page lists the tape", r.status_code == 200 and "First Tape" in r.text)
r = fan.get("/genres")
check("genres index lists Hip-Hop", "Hip-Hop" in r.text)
r = artist.post("/profile", data={"city": "Chicago, IL", "vanity": "test_artist"}, follow_redirects=False)
check("profile save works", r.status_code == 303, str(r.status_code))
r = fan.get("/city/chicago-il")
check("city page lists hometown tapes", r.status_code == 200 and "First Tape" in r.text)
artist2 = TestClient(demhits.app)
signup(artist2, "Second Artist", "artist2@test.local", "artist")
r = artist2.post("/profile", data={"city": "", "vanity": "test_artist"}, follow_redirects=False)
check("taken vanity link rejected", r.status_code == 400, str(r.status_code))
r = fan.post("/profile", data={"city": "", "vanity": "tapeking"}, follow_redirects=False)
r = anon.get("/@tapeking")
check("fan vanity claim did not take", r.status_code == 404, str(r.status_code))

r = artist.post("/upload", data={"title": "Hosted Tape", "genre": "Hip-Hop", "description": "d",
                                 "download_mode": "free", "price": "10", "rights": "yes",
                                 "hosted_by": "DJ Test"},
                files=[("cover", ("cover.png", PNG_COVER, "image/png")),
                       ("tracks", ("song.wav", WAV1, "audio/wav"))], follow_redirects=False)
tape3 = int(r.headers["location"].rstrip("/").split("/")[-1])
r = fan.get(f"/tape/{tape3}")
check("hosted-by credit shows on the tape", "DJ Test" in r.text)

r = fan.post(f"/comment/{tape1}", data={"body": "Fire tape, instant classic"}, follow_redirects=False)
check("comment posts", r.status_code == 303, str(r.status_code))
r = fan.get(f"/tape/{tape1}")
check("comment shows on tape page", "Fire tape, instant classic" in r.text)
r = anon.post(f"/comment/{tape1}", data={"body": "anon"}, follow_redirects=False)
check("anonymous comment bounces to login", r.status_code == 303 and "/login" in r.headers["location"])
conn = db()
cid = conn.execute("SELECT id FROM comments WHERE tape_id=?", (tape1,)).fetchone()[0]
conn.close()
r = fan2.post(f"/comment/{cid}/delete", follow_redirects=False)
check("cannot delete someone else's comment", r.status_code == 403, str(r.status_code))
r = fan.post(f"/comment/{cid}/delete", follow_redirects=False)
r = fan.get(f"/tape/{tape1}")
check("own comment deleted", "Fire tape, instant classic" not in r.text)

future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
r = artist.post("/upload", data={"title": "Premiere Tape", "genre": "Hip-Hop", "description": "d",
                                 "download_mode": "free", "price": "10", "rights": "yes",
                                 "release_at_utc": future},
                files=[("cover", ("cover.png", PNG_COVER, "image/png")),
                       ("tracks", ("song.wav", WAV1, "audio/wav"))], follow_redirects=False)
tape4 = int(r.headers["location"].rstrip("/").split("/")[-1])
r = fan.get(f"/tape/{tape4}")
check("premiere page shows countdown + notify", "PREMIERE" in r.text and "Notify Me" in r.text)
r = fan.post(f"/play/{tape4}")
check("plays do not count before the drop", r.status_code == 403, str(r.status_code))
conn = db()
trk4 = conn.execute("SELECT id FROM tracks WHERE tape_id=?", (tape4,)).fetchone()[0]
conn.close()
r = fan.get(f"/stream/{trk4}")
check("streaming blocked before the drop", r.status_code == 403, str(r.status_code))
r = fan.post(f"/premiere-notify/{tape4}", follow_redirects=False)
check("notify-me signup works", r.status_code == 303, str(r.status_code))
conn = db()
conn.execute("UPDATE mixtapes SET release_at=? WHERE id=?",
             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), tape4))
conn.commit()
conn.close()
r = fan.get(f"/tape/{tape4}")
check("tape goes live at drop time", "Notify Me When It Drops" not in r.text)
r = fan.get(f"/stream/{trk4}")
check("streaming opens at drop time", r.status_code == 200, str(r.status_code))
r = fan.get("/notifications")
check("drop alert delivered to waiting fan", "PREMIERE LIVE" in r.text)

r = fan.post("/playlists", data={"title": "My Crate"}, follow_redirects=False)
check("playlist created", r.status_code == 303, str(r.status_code))
conn = db()
pid = conn.execute("SELECT id FROM playlists WHERE title='My Crate'").fetchone()[0]
conn.close()
r = fan.post("/playlist-add", data={"playlist_id": pid, "tape_id": tape1}, follow_redirects=False)
check("tape added to playlist", r.status_code == 303, str(r.status_code))
r = anon.get(f"/playlist/{pid}")
check("public playlist page shows the tape", r.status_code == 200 and "First Tape" in r.text)
r = artist.post("/playlist-add", data={"playlist_id": pid, "tape_id": tape2}, follow_redirects=False)
check("cannot add to someone else's playlist", r.status_code == 403, str(r.status_code))

r = artist.post("/versus/start", data={"tape_a": tape1, "tape_b": tape2}, follow_redirects=False)
check("versus battle starts", r.status_code == 303, str(r.status_code))
conn = db()
bid = conn.execute("SELECT id FROM battles ORDER BY id DESC LIMIT 1").fetchone()[0]
conn.close()
r = fan.post(f"/versus/{bid}/vote", data={"tape_id": tape1}, follow_redirects=False)
check("fan vote accepted", r.status_code == 303, str(r.status_code))
r = fan.post(f"/versus/{bid}/vote", data={"tape_id": tape2}, follow_redirects=False)
conn = db()
nv = conn.execute("SELECT COUNT(*) FROM battle_votes WHERE battle_id=?", (bid,)).fetchone()[0]
conn.close()
check("one vote per fan in a battle", nv == 1, str(nv))
r = artist.post("/versus/start", data={"tape_a": tape1, "tape_b": tape3}, follow_redirects=False)
check("tape already in a battle cannot start another", r.status_code == 400, str(r.status_code))
conn = db()
conn.execute("UPDATE battles SET ends_at=? WHERE id=?",
             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), bid))
conn.commit()
conn.close()
r = fan.get("/versus")
check("finished battle shows a verdict", "Winner decided" in r.text)
r = fan.get(f"/tape/{tape1}")
check("versus winner crown shows on the winning tape", "VERSUS WINNER" in r.text)

r = fan.get("/wall-of-paid")
check("wall of paid shows the real fulfilled payout", "$8.00" in r.text and "Test Artist" in r.text)
r = anon.get("/sitemap.xml")
check("sitemap lists live tapes", r.status_code == 200 and f"/tape/{tape1}" in r.text)
r = anon.get("/robots.txt")
check("robots points to the sitemap", "sitemap.xml" in r.text)

# ---------------- wave 2: radio, recommendations, QR, supporters, open verse
r = anon.get("/radio")
check("radio page loads with stations", r.status_code == 200 and "HITZ Radio" in r.text and "Golden" in r.text and "dhRadio" in r.text)
r = anon.get("/")
check("home links the radio", "/radio" in r.text)
r = fan.get(f"/tape/{tape1}")
check("fans also played recommends other tapes", "Fans Also Played" in r.text and "Second Tape" in r.text)
r = anon.get(f"/qr/tape/{tape1}")
check("tape QR code serves an SVG", r.status_code == 200 and "<svg" in r.text)
conn = db()
artist_uid = conn.execute("SELECT id FROM users WHERE email='artist@test.local'").fetchone()[0]
conn.close()
r = anon.get(f"/qr/artist/{artist_uid}")
check("artist QR code serves an SVG", r.status_code == 200 and "<svg" in r.text)
r = anon.get("/qr/tape/99999")
check("QR for a missing tape is 404", r.status_code == 404, str(r.status_code))

# top supporter: fan tips, admin fulfills, badge appears
r = fan.post(f"/tip/{tape1}", data={"amount": 7}, follow_redirects=False)
check("tip request saved", r.status_code == 303, str(r.status_code))
r = admin.get("/api/admin/overview", headers={"X-Admin-Key": "test-admin-key"})
tip_tx = [p for p in r.json()["pending"] if p["kind"] == "tip" and p["tape_id"] == tape1][-1]
r = admin.post(f"/api/admin/fulfill/{tip_tx['id']}", headers={"X-Admin-Key": "test-admin-key"})
check("tip fulfilled via admin API", r.json().get("status") == "paid", r.text[:120])
r = fan.post(f"/comment/{tape1}", data={"body": "supporter check comment"}, follow_redirects=False)
r = fan.get(f"/tape/{tape1}")
check("top supporter badge shows by the fan's comment", "TOP SUPPORTER" in r.text)
r = fan.get(f"/artist/{artist_uid}")
check("artist page lists top supporters", "Top Supporters" in r.text and "Test Fan" in r.text)

# open verse: artist opens tape2, artist2 enters a response tape
r = artist.post(f"/tape/{tape2}/open-verse", data={"open": "1", "note": "Verse 2 is open"}, follow_redirects=False)
check("owner opens the verse", r.status_code == 303, str(r.status_code))
r = fan.post(f"/tape/{tape1}/open-verse", data={"open": "1", "note": "x"}, follow_redirects=False)
check("non-owner cannot open a verse", r.status_code == 403, str(r.status_code))
r = fan.get(f"/tape/{tape2}")
check("open verse shows on the tape", "OPEN VERSE" in r.text and "Verse 2 is open" in r.text)
r = upload(artist2, "Response Tape")
resp_id = int(r.headers["location"].rstrip("/").split("/")[-1])
r = artist2.post("/verse-entry", data={"original_id": tape2, "entry_id": resp_id}, follow_redirects=False)
check("verse entry accepted", r.status_code == 303, str(r.status_code))
r = fan.get(f"/tape/{tape2}")
check("entry listed on the original tape", "Response Tape" in r.text)
r = fan.get(f"/tape/{resp_id}")
check("entry banner on the response tape", "Open Verse entry" in r.text)
r = artist2.post("/verse-entry", data={"original_id": tape2, "entry_id": resp_id}, follow_redirects=False)
check("same tape cannot enter twice", r.status_code == 400, str(r.status_code))
r = artist2.post("/verse-entry", data={"original_id": tape2, "entry_id": tape1}, follow_redirects=False)
check("cannot enter someone else's tape", r.status_code == 403, str(r.status_code))
r = artist.post(f"/tape/{tape2}/open-verse", data={"open": "0", "note": ""}, follow_redirects=False)
r = upload(artist2, "Late Tape")
late_id = int(r.headers["location"].rstrip("/").split("/")[-1])
r = artist2.post("/verse-entry", data={"original_id": tape2, "entry_id": late_id}, follow_redirects=False)
check("closed verse takes no entries", r.status_code == 400, str(r.status_code))

# ------------------------------------------------ singles (Wave 3)
def upload_kind(client, title, kind, tracks):
    files = [("cover", ("cover.png", PNG_COVER, "image/png"))]
    for t in tracks:
        files.append(("tracks", t))
    data = {"title": title, "genre": "Hip-Hop", "description": "test",
            "download_mode": "free", "price": "10", "rights": "yes", "kind": kind}
    return client.post("/upload", data=data, files=files, follow_redirects=False)


r = artist.get("/upload")
check("upload page offers the single option", "SINGLE — just one song" in r.text)

r = upload_kind(artist, "Lone Single", "single", [("one.wav", WAV1, "audio/wav")])
check("single upload accepted", r.status_code == 303, str(r.status_code))
single_id = int(r.headers["location"].rstrip("/").split("/")[-1])
conn = db()
kind_row = conn.execute("SELECT kind FROM mixtapes WHERE id=?", (single_id,)).fetchone()
conn.close()
check("single stored as kind=single", kind_row[0] == "single", str(kind_row))

r = upload_kind(artist, "Fake Single", "single", [("a.wav", WAV1, "audio/wav"), ("b.wav", WAV2, "audio/wav")])
check("single with two tracks rejected 400", r.status_code == 400, str(r.status_code))

r = fan.get(f"/tape/{single_id}")
check("single page wears the SINGLE pill", "SINGLE" in r.text)

r = anon.get("/singles")
check("singles page lists the single", r.status_code == 200 and "Lone Single" in r.text and "SINGLE" in r.text)
check("singles page keeps mixtapes out", "First Tape" not in r.text)

r = anon.get("/")
check("homepage has a Fresh Singles section", "Fresh Singles" in r.text and "Lone Single" in r.text)

r = anon.get("/charts")
check("charts have a Top Singles board", "Top Singles" in r.text)

r = fan.post(f"/play/{single_id}")
check("single play counts", r.status_code == 200 and r.json()["counted"] is True)
r = anon.get("/singles")
check("most-played single wears GOLDEN SINGLE", "GOLDEN SINGLE" in r.text)
r = fan.get(f"/tape/{single_id}")
check("golden single pill on its own page", "GOLDEN SINGLE" in r.text)
r = fan.get(f"/tape/{tape1}")
check("golden single does not leak onto a tape page", "GOLDEN SINGLE" not in r.text)

print(f"\nALL {len(PASS)} CHECKS PASSED")
