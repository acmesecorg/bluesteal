# Blue Steal pairing rendezvous

A ~60-line Cloudflare Worker that lets two operators **in different rooms**
exchange a pairing token without reading each other's screen. It is the
out-of-band channel from [`../../specs/PAIRING_TOKEN.md`](../../specs/PAIRING_TOKEN.md):
it carries only the `CODE-ROUTING` token, never authentication data. The BLE
advert still relays over FIDO's own tunnel servers — this only moves the
server-assigned `routing_id` (which rotates ~every 60s, so entries are
short-lived by design).

## Wire contract

Base URL: `https://<your-worker>.workers.dev` (your deployed Worker).

| Method | Path | Body | Result |
|---|---|---|---|
| `PUT` / `POST` | `/r/{room}` | `{"token":"GXEVC-AFEA0C"}` | `{"ok":true,"ttl":120}` — stores for 120s |
| `GET` | `/r/{room}` | — | `{"token":"GXEVC-AFEA0C","ageMs":4213}` or `404 {"error":"empty"}` |
| `DELETE` | `/r/{room}` | — | `{"ok":true}` |

- `room`: `[A-Za-z0-9_-]{3,32}` — a short label the two operators agree on out of band (e.g. `DEMO1`).
- `token`: validated against `^[A-Za-z0-9]{5}-[0-9A-Fa-f]{6}$`, so only well-formed pairing tokens are stored.
- Auth: if the Worker secret `RENDEZVOUS_KEY` is set, every request needs `Authorization: Bearer <key>`. If unset, the Worker runs open (local testing only).

## Deploy (owner only)

> **Deploys are yours.** Nothing in this directory is a credential: the KV id is
> a placeholder, and the bearer is a Worker secret, never a file. This repo is
> public — do not commit `account_id`, the bearer value, or a `.dev.vars` file.

```bash
cd tools/rendezvous
npx wrangler kv namespace create RENDEZVOUS   # paste the printed id into wrangler.toml
npx wrangler secret put RENDEZVOUS_KEY        # optional; pick a long random value
npx wrangler deploy
```

Local test without deploying:

```bash
cp .dev.vars.example .dev.vars   # optionally set RENDEZVOUS_KEY
npx wrangler dev
# in another shell:
curl -X PUT localhost:8787/r/DEMO1 -d '{"token":"BSTL1-7C4A9C"}'
curl localhost:8787/r/DEMO1
```

## How the clients use it

The host and peer apps read the base URL and bearer from **local, gitignored
config** — never hardcoded, never committed:

- **iOS**: supplied **at runtime** via the URL scheme
  `bluesteal://config?rv=<baseURL>&room=<room>&k=<bearer>` (open a tappable link
  or scan a QR of it — Safari's address bar won't launch a custom scheme). It is
  stored in UserDefaults, never compiled in. See the iOS README.
- **Android**: `local.properties` keys `rendezvous.baseUrl` / `rendezvous.key` /
  `rendezvous.room` (already gitignored), surfaced as `BuildConfig` fields.
  Template in `local.properties.example`. A **Room** button appears once set.

Flow: the host runs a persistent-host loop — each time the relay drops its
waiting socket (~60s) it reconnects, gets a fresh `routing_id`/token, and `PUT`s
the new token under the agreed room. The peer polls `GET /r/{room}` and joins
with the latest token, retrying if a join races a rotation.
