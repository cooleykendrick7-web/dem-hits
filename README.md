# HITZ — THE REAL MIXTAPE CONNECT

Ken's mixtape platform: artists post mixtapes **free**, fans stream free, and
artists actually get paid — paid downloads (artist keeps 80%, 85% on Pro),
tips (90%), fan subscriptions (80%), and stream share — with discovery,
charts and sponsored placement built in from day one.

FastAPI + SQLite, single `app.py`, real audio file hosting and streaming
(HTTP Range, so the player can seek), persistent bottom player.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ADMIN_PIN=pick-a-pin SESSION_SECRET=pick-a-long-secret \
  .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — `/healthz`, `/get-paid`, `/faq`, `/charts`,
`/admin` (PIN login).

## Tests

```bash
.venv/bin/pip install httpx   # test-only dependency
.venv/bin/python tests/run_tests.py
```

31 checks: auth roles, rights attestation, audio validation, real streaming
+ Range, play dedupe, all three download modes, money splits, fulfillment
gating, earnings Paid vs Pending. Tests use a temp DATA_DIR — production
data is never touched, and the production DB starts empty on purpose
(no fake artists, tapes, plays, or earnings).

## Environment variables

| Var | Purpose |
|---|---|
| `ADMIN_PIN` | Owner/Admin login PIN. Default in code is a dev placeholder — **always set in production**. Keep the real value outside the repo. |
| `SESSION_SECRET` | Signs the admin cookie. Set a long random value in production. |
| `DATA_DIR` (or `UPLOAD_DIR`) | Where the SQLite DB and uploaded audio/covers live. Default `./data`. Point it at a persistent disk mount in production. |
| `STRIPE_SECRET_KEY` | Optional. **Until Ken connects his own Stripe, leave unset:** every money action (tips, paid downloads, subs, Sponsored, Pro, Premium) is saved as an `unpaid_request` labeled "payments being connected" and fulfilled by hand in Admin after verified payment. Nothing is ever faked as charged or paid. |
| `BASE_URL` | Public base URL of the deployment (for future share/canonical links). |

## Deploy (Render)

1. Create the GitHub repo and push this folder (done in the browser phase).
2. Render → New → Blueprint, select the repo — `render.yaml` defines the
   free web service (`uvicorn app:app --host 0.0.0 --port $PORT`).
3. Set env vars in Render: `ADMIN_PIN`, `SESSION_SECRET`, `BASE_URL`,
   `DATA_DIR` (see caveat), `STRIPE_SECRET_KEY` later when Stripe is connected.
4. Deploy, then check `/healthz`.

### ⚠️ Free-tier caveat (read before advertising)

Render's free tier has **no persistent disk**: the SQLite DB and all
uploaded audio reset on redeploy/restart. Before taking real artist
volume, upgrade to a paid plan with a persistent disk mounted at
`DATA_DIR` (e.g. `/var/data`) — no code change needed — or move uploads
to object storage. The storage layer is already isolated behind
`DATA_DIR` for exactly this.

## Money rules (server-enforced)

- Uploading is **free** for artists, forever. Pro / Sponsored / Premium
  are optional upgrades, never a paywall to post or earn.
- Splits: paid downloads 80/20 (85/15 Pro), tips 90/10, fan subs 80/20.
  Stripe fees come out before the split when Stripe is live.
- Sponsored: $49/3d, $99/7d, $199/14d — max **5** active homepage spots;
  extra fulfilled requests waitlist in Admin.
- Paid downloads count only after Admin fulfillment; free downloads
  count once per fan and auto-follow the artist; Stream Only tapes
  reject downloads for everyone, Premium included.
- Plays dedupe per listener per tape per day.
- Cash-out threshold $25, shown in the artist dashboard. No
  guaranteed-income language anywhere — the calculator is labeled an
  example estimate.
