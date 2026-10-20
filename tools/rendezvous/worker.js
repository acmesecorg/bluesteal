// Blue Steal pairing rendezvous — a minimal KV-backed token drop.
//
// Two separated operators agree on a short ROOM out of band (e.g. "DEMO1").
// The host PUTs its current pairing token under that room; the peer GETs it.
// This is the out-of-band channel the PAIRING_TOKEN spec describes — it carries
// only the CODE-ROUTING token, never authentication data. The actual BLE advert
// still relays over FIDO's own tunnel servers; this just lets two people in
// different rooms exchange the server-assigned routing_id (which rotates ~every
// 60s, so entries are short-lived by design).
//
// NO CREDENTIALS LIVE IN THIS REPO. The optional bearer is a Worker secret
// (RENDEZVOUS_KEY) set via `wrangler secret put`, read from env at runtime, and
// never written to a file. Deploy is the repo owner's own action (see README).

const ROOM_RE = /^[A-Za-z0-9_-]{3,32}$/;
const TOKEN_RE = /^[A-Za-z0-9]{5}-[0-9A-Fa-f]{6}$/; // CODE-ROUTING, per PAIRING_TOKEN.md
const TTL_SECONDS = 120;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/r\/([^/]+)\/?$/);
    if (!match) return json({ error: "not_found" }, 404);

    const room = decodeURIComponent(match[1]);
    if (!ROOM_RE.test(room)) return json({ error: "bad_room" }, 400);

    // Optional bearer auth — enforced only when the secret is configured, so a
    // local `wrangler dev` with no secret runs open for quick testing.
    if (env.RENDEZVOUS_KEY) {
      const header = request.headers.get("Authorization") || "";
      const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
      if (!timingSafeEqual(presented, env.RENDEZVOUS_KEY)) {
        return json({ error: "unauthorized" }, 401);
      }
    }

    switch (request.method) {
      case "PUT":
      case "POST": {
        let body;
        try {
          body = await request.json();
        } catch {
          return json({ error: "bad_json" }, 400);
        }
        const token = typeof body?.token === "string" ? body.token.trim() : "";
        if (!TOKEN_RE.test(token)) return json({ error: "bad_token" }, 400);
        const value = JSON.stringify({ token, ts: Date.now() });
        await env.RENDEZVOUS.put(room, value, { expirationTtl: TTL_SECONDS });
        return json({ ok: true, ttl: TTL_SECONDS });
      }

      case "GET": {
        const value = await env.RENDEZVOUS.get(room);
        if (!value) return json({ error: "empty" }, 404);
        const { token, ts } = JSON.parse(value);
        return json({ token, ageMs: Date.now() - ts });
      }

      case "DELETE": {
        await env.RENDEZVOUS.delete(room);
        return json({ ok: true });
      }

      default:
        return json({ error: "method_not_allowed" }, 405);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// Constant-time compare so the bearer check doesn't leak length/content timing.
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
