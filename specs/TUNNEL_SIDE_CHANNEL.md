# Blue steal Side-Channel via caBLE Tunnel Server (Feasibility & Implementation)

## Verdict: feasible, with one load-bearing assumption to confirm empirically in step 1

The caBLE tunnel servers (`cable.ua5v.com`, `cable.auth.com`, and the hashed
domain family) are pure WebSocket bridges. They pair an "authenticator"
connection on `wss://<host>/cable/new/<HEX(tunnel_id)>` (subprotocol
`fido.cable`) with a "consumer" connection on
`wss://<host>/cable/connect/<routing_id_hex>/<tunnel_id_hex>`, matched on
`tunnel_id`. Once both sides are present, every binary frame from one is
delivered to the other. There is **no FIDO-aware inspection in the server** —
it cannot tell whether the framed bytes are Noise/CTAP2 or anything else.
That is exactly what blue steal needs: a free, NAT-traversing,
TLS-terminated, low-latency byte pipe that two endpoints can rendezvous on
if they share one secret (the existing 5-char pairing code).

What makes this work for our use case:

- The **5-char code** can deterministically derive `tunnel_server_id` (so
  both sides pick the same hostname) and `tunnel_id` (so both sides hit
  the same bridging slot) — no second out-of-band exchange needed, no
  MQTT, no STUN/TURN.
- The host plays the **authenticator role**: opens `/cable/new/<tid>`,
  reads `X-caBLE-Routing-ID`. It does **not** need to put the EID on BLE
  (this is blue steal traffic, not real FIDO traffic) — the routing ID is
  only needed by the server to know which downstream node holds the
  publisher socket.
- The peer plays the **Chromium consumer role**: opens
  `/cable/connect/<routing>/<tid>`. The routing_id chosen by the peer
  needs to land it on the same physical server as the host; on the
  public Google/Apple tunnel servers the routing is single-tier (one DNS
  name → one cluster), so `000000` (Chromium's own fallback) is expected
  to suffice. **Step 1 of the plan verifies this** before we build
  anything substantial.
- The 1:1 server bridging is already the design point — Chromium's
  `FidoTunnelDevice` and the Rust authenticator's (original open source implementation) `Tunnel::connect` both
  consume it this way.

What's already in-house (not currently open source):

- `VirtualFido.CrossPlatform.Cable.TunnelServerDomain` — `Get(ushort id)
  → host` with the salt/SHA-256/base-32 derivation. Bluesteal can either
  project-ref `VirtualFido.CrossPlatform` or vendor the ~70-line file.
- `VirtualFido.CrossPlatform.Cable.TunnelClient` — `ClientWebSocket`
  against `wss://<host>/cable/new/<HEX(tunnelId)>`, subprotocol
  `fido.cable`, `X-caBLE-Routing-ID` header parse, inbound
  `Channel<byte[]>`, `SendAsync`/`ReceiveAsync`. This is exactly the
  host-side socket blue steal needs.
- `VirtualFido.Crypto.CableNoise.{Noise, HandshakeResponder, Crypter,
  P256}` — full Noise NKpsk0 responder + the dual-construction
  post-handshake `Crypter`. For framing-preservation between the two
  blue steal endpoints this is the engine; we'd also need a thin
  **initiator** counterpart (Chromium has `BuildHandshakeMessage`; Rust
  has `build_initiator` in `cable/noise.rs`) — straightforward to add as
  a peer of `HandshakeResponder.cs`. (Look at `chromium
  source/v2_handshake.cc` `BuildHandshakeMessage` for the 5-line spec.)
- `Bluesteal.Services.RelayClient` / `AdvertRelay` already define the
  abstract "byte-pipe with framed advert messages" boundary that the new
  transport plugs into. Specifically, the surface used elsewhere is just
  `RelayState`, `OnMessage`, `Send(RelayMessage)`, and the
  `PropertyChanged` ticks — we can keep that interface intact and swap
  the transport behind it.

What's new in blue steal:

1. A `TunnelTransport` (sibling of today's `SignalingClient` + WebRTC
   stack) that does the deterministic derivation, opens the right socket
   per role, runs Noise, and exposes the same `Send`/`OnMessage` shape.
2. A small **Noise NKpsk0 initiator** in
   `VirtualFido.CrossPlatform.CableNoise` (or vendored into blue steal)
   so the peer side can complete the handshake the host's existing
   `HandshakeResponder` is waiting for.
3. A `TransportKind` enum on `RelayClient` (`WebRtc` | `CableTunnel`)
   and UI plumbing in `RelayView.xaml.cs` to choose between them. WebRTC
   stays as the default per requirement (5).
4. A new pair-secret derivation (the 5-char code → `tunnel_server_id`,
   `tunnel_id`, `static_keypair_seed`, `psk`) so both sides agree
   without any QR.

## Architectural mapping (which caBLE field is what, in blue steal terms)

> For a plain-English explainer of the user-facing `CODE-ROUTING` pairing
> token — why it has both a secret prefix and a server-assigned routing
> suffix, and why neither can be stripped — see
> [`PAIRING_TOKEN.md`](PAIRING_TOKEN.md). Note the `routing_id` row below
> (`000000` fallback) is the pre-probe assumption, superseded by the Step 1
> findings further down.

Because there is no real FIDO QR in blue steal, we synthesise the
cryptographic inputs from the 5-char code so the existing caBLE
primitives "just work":

| caBLE concept | Real FIDO source | Blue steal source |
|---|---|---|
| `tunnel_server_id` (u16) | derived from `qr_secret` via HKDF info=2 in the spec, but actually picked by the authenticator from `num_known_domains` in the QR | `HKDF(code_utf8, info="bluesteal/tunnel-server-id") mod 256`, clamped to a stable assigned id (0 = Google) for the first cut |
| `tunnel_id` (16 B) | `HKDF(qr_secret, info=LE32(2))` | `HKDF(code_utf8, info="bluesteal/tunnel-id", L=16)` |
| `psk` (32 B) | `HKDF(qr_secret, salt=eid_bytes, info=LE32(3))` | `HKDF(code_utf8, info="bluesteal/psk", L=32)` |
| Responder static key (NKpsk0 `s`) | persistent EC key, public half encoded in QR field 0 | `HKDF(code_utf8, info="bluesteal/responder-static", L=32)` → P-256 scalar; both sides derive identical keypair so initiator's pre-message `MixHash(s_pub)` matches |
| `routing_id` (3 B) | server-assigned in `X-caBLE-Routing-ID` header | host learns it from header; peer ignores its own value, uses Chromium-style `000000` to connect (Step 1 verifies bridging) |
| EID + BLE advert | broadcast over 0xFFF9 | **not used** — bluesteal carries no FIDO traffic, just its own framed `RelayMessage`s |

Note that the responder static key being **derived from a shared
secret** is a deviation from real NKpsk0's threat model (the initiator
normally learns the responder's public key from a trusted OOB channel).
Inside bluesteal this is fine — we're using NKpsk0 as a convenient AEAD
framing layer, not as authenticator authentication — but it must be
called out in the code comments so a future reader doesn't think we're
claiming FIDO-grade security.

## Wire format inside the tunnel

After the Noise handshake completes and `Crypter.UseNewConstruction()`
fires on the host's first outbound frame, both sides have an AEAD
send/recv pair. We then ship the *existing* `RelayMessage` JSON (the
same payload `RelayClient.Send` already encodes for WebRTC) as the
plaintext of each frame. Optionally, we wrap it in the existing
CTAP-style `Ctap2Framing` byte (type `0x01`) so the spec's
heartbeat/shutdown markers are reusable; for v1 just lift the JSON body
1:1 is fine.

This preserves framing (requirement 4) and means `AdvertRelay`,
`RebroadcastService`, heartbeats, watchdog, and bye-handling all keep
working unmodified.

## Feasibility caveats worth surfacing in the plan, not hiding

1. **Server bridging across routing_ids on the same host** is the
   load-bearing assumption. If `cable.ua5v.com` shards internally and
   refuses to pair a `/cable/new/<tid>` on one shard with
   `/cable/connect/000000/<tid>` on another, Step 1 will show it and we
   either: (a) probe the actual routing_id by retry, or (b) reuse the
   existing MQTT signaling channel just to ferry the 3-byte routing_id
   (a 6-hex-char message, far smaller than the offer/answer/ICE traffic
   it replaces) — still a huge net win.
2. **Terms of use.** `cable.ua5v.com` is operated by Google for FIDO
   traffic. Using it as a generic bridge for non-FIDO bytes is
   technically possible because the server can't see inside Noise, but
   it is **off-label use** and Google could rate-limit, fingerprint
   (`Origin`, idle patterns, frame sizes), or block it at any time.
   This should be a documented operational risk on the new transport's
   view, not a silent dependency. `cable.auth.com` is Apple; same
   concern.
3. **Session lifetime.** Real caBLE sessions are short (seconds). The
   tunnel server may impose an idle timeout that's shorter than
   blue steal's typical session. We need to measure and, if needed, run
   heartbeats at <30 s spacing (the existing `AdvertRelay` heartbeat is
   4 s — already well within any plausible limit).
4. **Concurrent sessions.** Two blue steal users with the same 5-char
   code at the same time would collide on `tunnel_id`. This is the same
   property the existing MQTT-topic approach has, but on MQTT a third
   party could observe; on the tunnel server they can only collide.
   Document in the README.
5. **The "second tunnel during authentication" framing in the user's
   question** — i.e. that a real iCloud-Keychain caBLE session is in
   flight on a different `tunnel_id` and blue steal opens its own session
   alongside it — is fine and was the original mental model. Nothing
   about our use of the server conflicts with another session, because
   `tunnel_id` partitions them.

## File-level shape of the change in `bluesteal-windows`

New, under `Bluesteal/Services/Tunnel/`:

- `TunnelKeyDerivation.cs` — single static class, all HKDFs from the
  5-char code.
- `TunnelTransport.cs` — public surface mirrors `RelayClient`'s
  consumer-facing methods (`HostAsync`, `JoinAsync(code)`, `Send`,
  `OnMessage`, `StateChanged`); internally calls the next two.
- `TunnelHostSocket.cs` — wraps `VirtualFido.CrossPlatform.Cable.TunnelClient`
  or a vendored copy (`/cable/new/<tid>`, reads `X-caBLE-Routing-ID`).
- `TunnelPeerSocket.cs` — new `ClientWebSocket` against
  `/cable/connect/<routing>/<tid>` with subprotocol `fido.cable`. ~50
  lines; mirrors the host socket but different URL and no header
  parsing.
- `TunnelNoiseSession.cs` — runs NKpsk0 responder (host) or initiator
  (peer) using `VirtualFido.Crypto.CableNoise`, then exposes
  encrypt/decrypt over `Channel<byte[]>` like `TunnelClient` does today.

Modified:

- `Bluesteal/Services/RelayClient.cs` — add `TransportKind { WebRtc,
  CableTunnel }`, branch the `Host()` / `Join()` paths between the
  existing `SignalingClient`+SIPSorcery stack and the new
  `TunnelTransport`. The post-`Open` behaviour (DataChannel/Inbound
  channel → `OnMessage`) is unified behind a thin internal `ITransport`
  interface.
- `Bluesteal/Bluesteal.csproj` — `ProjectReference` to
  `VirtualFido.CrossPlatform` (or, if cross-repo coupling is unwanted,
  vendor `TunnelServerDomain.cs`, `TunnelClient.cs`, and the four
  `CableNoise` files into a new `Bluesteal/Crypto/Cable/` folder — ~600
  lines total and self-contained, no upstream dependencies beyond
  .NET).
- `Bluesteal/Views/RelayView.xaml` + `.cs` — radio buttons or combo to
  select transport on the host side; peer auto-discovers from a code
  prefix or new "transport" suffix on the code if mixed deployments
  matter (otherwise both sides just need to pick the same transport —
  same UX as today's broker-selection-by-hash).
- `Bluesteal/App.xaml.cs` — DI registration for the new services.

Not modified at all:

- `BleAdvertiser`, `AdvertRelay`, `RebroadcastService`, `AdvertStore`,
  `CableScanner`, `AdvertClassifier`, all `Models/*`, all
  `ViewModels/*`. The transport boundary is exactly the abstraction
  line.

## Risks / open questions to settle during execution

- **VirtualFido coupling**: project-ref vs vendor. Recommend vendor (a
  single explicit copy under `Bluesteal/Crypto/Cable/`) so blue steal
  stays buildable without the sibling VirtualFido repo. Annotate with
  `// origin: VirtualFido.CrossPlatform/Cable/TunnelClient.cs @ <sha>`
  headers for traceability.
- **NKpsk0 initiator**: not present in VirtualFido today (it only ships
  the responder, because the authenticator is always the responder in
  real caBLE). Add `HandshakeInitiator.cs` next to
  `HandshakeResponder.cs`; the algorithm is a literal translation of
  the 8 lines in `chromium source/v2_handshake.cc::BuildHandshakeMessage`.
  Unit-test with a loopback against `HandshakeResponder`.
- **`Origin` header** sent by `ClientWebSocket`. Chromium sends
  `chrome-extension://...` or empty; real FIDO authenticators send
  nothing. Mirror Chromium's behaviour on the peer socket and the Rust
  authenticator's behaviour on the host socket to minimise
  fingerprinting differential.
- **Telemetry**: reuse `RelayTracer` so the new transport's logs land in
  the same JSONL file. Add `transport=cable-tunnel` and a
  `tunnel_server_host` field on every line.

## Steps

1. **Empirically verify same-tunnel bridging on the public servers**
   before writing production code. Write a throw-away console probe in a
   new `tools/TunnelProbe/` (or a one-shot `dotnet run` xUnit) that:
   derives `tunnel_id` from a hard-coded 5-char code, opens
   `/cable/new/<tid>` against `cable.ua5v.com`, captures
   `X-caBLE-Routing-ID`; from a second process opens
   `/cable/connect/000000/<tid>` against the same host; sends a 4-byte
   ping on each side and asserts it arrives on the other within 2 s.
   Repeat with the assigned routing_id on the peer. Record both outcomes
   in `docs/tunnel-side-channel.md`. **If neither routing_id works,
   escalate**: the design needs a 6-hex-char routing_id hop over the
   existing MQTT signaling (still a major simplification over WebRTC)
   before the rest of the plan proceeds.
2. **Vendor the C# caBLE primitives into blue steal** under
   `Bluesteal/Crypto/Cable/` and `Bluesteal/Crypto/CableNoise/`. Copy
   `TunnelServerDomain.cs`, `TunnelClient.cs`, `CableEid.cs` (only if
   needed for HKDF helpers — likely not), and the four `CableNoise`
   files (`Noise.cs`, `P256.cs`, `HandshakeResponder.cs`, `Crypter.cs`)
   from
   `C:\Users\James.Westgate\source\repos\VirtualFido\src\VirtualFido.CrossPlatform\`.
   Update namespaces to `Bluesteal.Crypto.*`. Add origin-header
   comments. Compile clean.
3. **Add `HandshakeInitiator.cs`** beside `HandshakeResponder.cs`. Port
   Chromium's `BuildHandshakeMessage` + the corresponding split
   (initiator: `send=k1, recv=k2`) per `hybrid-tunnel-spec.md` §4. Write
   a unit test `HandshakeLoopbackTests` that wires
   initiator↔responder in memory with a shared PSK + responder static
   key and asserts they derive matching `Crypter`s.
4. **Implement `Bluesteal/Services/Tunnel/TunnelKeyDerivation.cs`** —
   single static class with `Derive(string code) → (ushort
   tunnelServerId, byte[16] tunnelId, byte[32] psk, ECDsa
   responderStatic)`. Use `HKDF.DeriveKey(HashAlgorithmName.SHA256, ikm:
   utf8(code), salt: empty, info: utf8(label), outputLength: …)` with
   distinct labels per output. Unit tests: same code → identical
   outputs; different codes → differ in every byte.
5. **Implement `Bluesteal/Services/Tunnel/TunnelHostSocket.cs`** — thin
   wrapper around the vendored `TunnelClient` that opens
   `/cable/new/<HEX(tunnel_id)>` and exposes `RoutingId`, `Inbound`
   channel, `SendAsync`, `DisposeAsync`. Surface failures via the same
   `RelayTracer` event names the existing stack uses (`ws_connecting`,
   `ws_open`, etc.).
6. **Implement `Bluesteal/Services/Tunnel/TunnelPeerSocket.cs`** —
   `ClientWebSocket` to
   `wss://<host>/cable/connect/<routing_hex>/<tid_hex>`, subprotocol
   `fido.cable`, same inbound `Channel<byte[]>` shape. Default
   `routing_hex = "000000"`; expose a constructor overload taking a
   3-byte routing_id for the "fallback over MQTT" path if Step 1 forces
   it.
7. **Implement `Bluesteal/Services/Tunnel/TunnelNoiseSession.cs`** —
   given an opened socket and the `TunnelKeyDerivation` output, runs
   NKpsk0 (responder on host, initiator on peer), then exposes an
   encrypted `Send(byte[] pt)` / `OnReceived` API by wrapping the
   `Crypter` and the socket's I/O channels. Mirror
   `VirtualFido.CrossPlatform.Cable.HybridSession`'s post-handshake loop
   but without CTAP framing — just AEAD-frame the raw bluesteal payload
   bytes.
8. **Define `Bluesteal/Services/ITransport.cs`** — small interface:
   `Task ConnectAsync`, `Task Send(byte[])`, `event Action<byte[]>
   OnFrame`, `event Action<RelayState> StateChanged`, `Task
   DisposeAsync`. Make both the existing WebRTC stack and the new
   `TunnelTransport` implement it. Refactor `RelayClient` to depend on
   `ITransport` rather than directly on `SignalingClient` + SIPSorcery
   types. No behaviour change for the WebRTC path.
9. **Implement `Bluesteal/Services/Tunnel/TunnelTransport.cs`**
   implementing `ITransport`. Composes `TunnelKeyDerivation` +
   `TunnelHostSocket`/`TunnelPeerSocket` + `TunnelNoiseSession` per
   role. Emits `RelayState` transitions (`Hosting`/`Joining` →
   `Negotiating` after WS open → `Open` after Noise split → `Closed` on
   socket/Noise error). Hook into `RelayTracer` with
   `transport=cable-tunnel` on every line.
10. **Wire transport selection into `RelayClient` and the UI.** Add
	`RelayClient.TransportKind` (default `WebRtc` to preserve
	requirement 5). In `RelayView.xaml`, add a radio group "Pairing
	transport: WebRTC (default) / caBLE side-channel"; persist last
	choice. Both sides must pick the same kind — keep the existing
	5-char code, but for the tunnel transport encode the kind in a
	single prefix character (e.g. `T` + 4 base32 chars vs `W` + 4) so a
	mis-paired join fails fast with a clear error rather than hanging
	on an absent peer.
11. **Add an integration test** `TunnelTransportLoopbackTests` that
	spins two `TunnelTransport` instances (host + peer) against the
	**real public tunnel server** behind a `[Trait("Category","Network")]`
	gate, runs the Noise handshake, exchanges a known
	`RelayMessage.Heartbeat` round-trip, and asserts the receive side
	decodes identical JSON. Keep this opt-in (not in CI default) due to
	the off-label-use risk; document the gate in `tests/README.md`.
12. **Document the new transport** in `docs/tunnel-side-channel.md`:
	the feasibility proof from Step 1, the derivation table, the wire
	format inside the tunnel, the off-label-use disclaimer, the
	rate-limit/abuse considerations, and the upgrade story for when the
	Authenticator iOS app gains its own host-role implementation (just
	swap `TunnelHostSocket` for the iOS native equivalent — the
	bluesteal peer socket and Noise initiator are unchanged).


---

## Step 1 � Empirical bridging probe (results)

**Date run:** 2026-05-22
**Probe:** `tools/TunnelProbe/Program.cs` (in `bluesteal-windows`)
**Host:** `cable.ua5v.com` (Google's public caBLE tunnel server)
**Code used:** `BSTL1` ? `tunnel_id = 377B0B4170FBAD1FEFEFFE0BAF6E3402`

### Raw output

```
[host] connecting to /cable/new/<tid> ...
[host] connected. negotiated subprotocol = fido.cable
[host] X-caBLE-Routing-ID = 3C1545
[host] Sec-WebSocket-Accept: mS6egfubB7Bsb2o2zSCEvbDkr2I=
[host] Sec-WebSocket-Protocol: fido.cable
[host] X-Cable-Routing-Id: 3c1545

--- attempt: routing=000000 (fallback) ---
[peer] PEER CONNECT FAILED � WebSocketException:
       The server returned status code '421' when status code '101' was expected.

--- attempt: routing=3C1545 (assigned) ---
[peer] connected. negotiated subprotocol = fido.cable
[host] -> peer : AABBCCDD
[peer] -> host : 11223344
[peer<-host] got: AABBCCDD  match=True
[host<-peer] got: 11223344  match=True

==== VERDICT ====
  routing=000000 (fallback) : PEER CONNECT FAILED � 421 Misdirected Request
  routing=3C1545 (assigned) : PASS � full duplex bridging works
```

### Findings

1. **Bridging itself works exactly as the spec implies.** Two `ClientWebSocket`s
   on the same `tunnel_id`, one on `/cable/new/<tid>` and one on
   `/cable/connect/<routing>/<tid>`, are paired by the server and every
   binary frame is delivered to the other side in full duplex.
2. **The Chromium-style `routing=000000` fallback does NOT work.** The
   server returns **HTTP 421 Misdirected Request**, which is exactly the
   failure mode the FIDO spec documents when the consumer hits a shard
   that doesn't hold the publisher socket. So Google's deployment is
   sharded, and the peer must know the *real* routing_id assigned to
   the host before it can join the bridge.
3. **The `X-caBLE-Routing-ID` response header is present and well-formed**
   (3 bytes, 6 hex chars � `3C1545` in this run). It also appears in the
   lowercase form `X-Cable-Routing-Id` in the response, which the
   client-side parsing must be case-insensitive about.
4. **WebSocket subprotocol `fido.cable` is negotiated correctly on both
   legs.** No TLS, hostname, SNI, or subprotocol surprises.

### Decision (supersedes the "Feasibility caveats" item 1 AND the MQTT-handoff sketch that previously occupied this slot)

The load-bearing assumption -- that `routing=000000` would suffice -- is
**falsified**. The peer MUST know the host's 3-byte routing_id before
it can connect. An earlier draft of this section proposed reusing the
existing MQTT broker to ferry that 6-hex-char value; that idea is now
also abandoned (see Probes B and C below) in favour of carrying the
routing_id in the **same out-of-band channel that already carries the
5-char code today** (WhatsApp/iMessage/verbal). MQTT, STUN, TURN, ICE,
SIPSorcery are **fully removed** from the cable-tunnel path.

## Step 1 -- Probes B & C: can we derive routing_id offline?

**Date run:** 2026-05-22
**Probe:** `tools/TunnelProbe/Program.cs` extended with two probes.
**Host:** `cable.ua5v.com`.

### Probe B -- determinism across repeated `/cable/new/<tid>` opens

Same `tunnel_id = 377B0B4170FBAD1FEFEFFE0BAF6E3402` (from code `BSTL1`),
five sequential opens with the prior socket closed before the next
open:

```
iter 1: routing = 7C4A9C
iter 2: routing = C8118D
iter 3: FAILED -- HTTP 409 Conflict
iter 4: routing = BA51F5
iter 5: FAILED -- HTTP 409 Conflict
```

**Findings:**

1. **routing_id is NOT a function of tunnel_id.** Three successful opens
   returned **three distinct** routing_ids. Hash-derivation is impossible
   -- there is no client-visible input the server uses; it is server-side
   pool allocation per session.
2. **The server holds `tunnel_id` for some grace period after close.**
   A second `/cable/new/<same tid>` opened too soon returns
   **HTTP 409 Conflict**, not a new session. This is a useful liveness
   signal -- the tunnel_id slot is single-host while the prior host
   session is still being torn down. (Iters 2 and 4 only succeeded
   because the previous slot had aged out.)

### Probe C -- routing_id distribution across distinct tunnel_ids

Sixteen different codes (`Z0000`..`Z0015`) -> sixteen distinct
tunnel_ids -> only **5 distinct routings** observed:
`3C1545, 0B56F0, 9E5864, C8118D, BA51F5`.

**Finding:** Google's deployment has a small pool of backend shards
(~5-8 visible here). routing_id is pool-allocated per session, not a
hash of anything we can know in advance.

### Final design: 11-char out-of-band token

Since the routing_id cannot be derived and we have already removed
WebRTC's signaling burden, we move the routing_id into the **same
out-of-band channel that already carries the 5-char code today**. The
human shares one slightly longer token instead of two separate things.
This deletes MQTT from the cable-tunnel path entirely.

**Token shape (v1, locked in):**

```
BSTL1-7C4A9C
^^^^^ ^^^^^^
code  routing_id (3 bytes, uppercase hex)
```

- Total **11 characters**, one dash separator.
- Prefix (`BSTL1`) is generated by the host exactly as today -- same
  alphabet, same `MakeCode` in `SignalingClient`.
- Suffix (`7C4A9C`) is the `X-caBLE-Routing-ID` value the host learns
  from the response headers of its `/cable/new/<tid>` open.
- The host opens its tunnel WSS **first**, learns the routing_id, then
  displays / shares the composite token. The peer parses the token,
  derives `tunnel_id` from the prefix, and opens
  `/cable/connect/<suffix>/<HEX(tunnel_id)>` directly.

**v1.1 future optimisation (not blocking):** a more human-friendly
encoding (e.g. base32-Crockford of the 8 bytes [5-char-utf8-hash || 3
byte routing], or a 4-word BIP-39-style triplet) can replace the
hex/dash form once the protocol is shipped. The wire/derivation rules
do not change.

### Knock-on changes to the rest of the plan

- **Step 4 (`TunnelKeyDerivation`)** -- add `TryParseToken(string token,
  out string code, out byte[3] routingId)` and a `FormatToken(code,
  routingId) -> string` helper. Validation: regex
  `^[A-Z0-9]{5}-[0-9A-F]{6}$`.
- **Step 6 (`TunnelPeerSocket`)** -- drop the `routing_hex = "000000"`
  default. Constructor mandatorily takes a 3-byte routing_id.
- **Step 9 (`TunnelTransport`)** -- host: after WSS open, surface the
  composite token via a new `RelayClient.PairingToken` property (the UI
  already shows the 5-char code; just change the label and the value).
  Peer: `JoinAsync(token)` parses, derives, opens. **No MQTT, no
  `SignalingClient` involvement in this path at all.**
- **Step 10 (UI)** -- the host's displayed value is the 11-char token;
  the peer's input field accepts either the 5-char code (WebRTC
  transport) or the 11-char token (cable-tunnel transport) and routes
  on shape. The transport-selection radio group becomes implicit on the
  join side and stays explicit on the host side.
- **Liveness of `tunnel_id`** -- the HTTP 409 behaviour means re-hosting
  on the same code immediately after a session ends will fail until
  the server's grace period elapses. Document this in the UI ("wait a
  moment before retrying") or have the host pick a fresh code on
  retry. Not blocking.

### Status

Step 1 is **complete**. The bridging works, the routing pool size is
acceptable, the OOB token shape is locked. Proceed to Step 2 (vendor
the C# caBLE primitives into bluesteal).
