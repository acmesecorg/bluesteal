# Pairing token anatomy — why it's `CODE-ROUTING`

## TL;DR

The pairing token a user reads across to the other device (e.g.
`GXEVC-AFEA0C`) has two halves that look similar but do completely
different jobs, and **neither can be removed**:

- **Prefix** (`GXEVC`, 5 chars) — the **shared secret**. Every
  cryptographic value is HKDF-derived from it. It is the caBLE `qr_secret`
  equivalent.
- **Suffix** (`AFEA0C`, 6 hex / 3 bytes) — the server-assigned
  **`routing_id`**. It carries no secret, can't be derived or predicted, and
  the peer can't reach the host's tunnel without it. It is the caBLE
  BLE-advert equivalent.

The `-` is **cosmetic** — auto-inserted in the entry field, stripped on
parse. It is not part of the protocol.

> A common misread: that the suffix is "the tunnel id from the cable
> protocol" and the prefix is a redundant local label. It's the reverse.
> The real 128-bit `tunnel_id` (`9F0B53AB…` in traces) is **derived from the
> prefix** and never appears in the token; the suffix is just a routing
> handle.

## Token anatomy

| | Prefix | `-` | Suffix |
|---|---|---|---|
| Example | `GXEVC` | `-` | `AFEA0C` |
| Shape | `[A-Z0-9]{5}` | literal | `[0-9A-F]{6}` |
| Meaning | shared secret (code) | display only | `routing_id` (3 bytes, hex) |
| Origin | random on host (`PairingCode.make`) | inserted in UI | server-assigned |

Full validation regex (canonical `TunnelKeyDerivation.cs:97`, mirrored in
Swift `TunnelKeyDerivation.swift` `matchToken`):

```
^([A-Z0-9]{5})-([0-9A-F]{6})$
```

The dash is auto-managed so the user never types it. `formatTokenInput`
(`bluesteal-macos/.../Views/RelayView.swift:236-244`) strips non-alphanumerics,
uppercases, caps at 11 raw chars, and inserts `-` at position 5. The
`TokenTextField` coordinator (`RelayView.swift:247-317`) preserves the caret
across that reformat by tracking the *logical* (alphanumeric) position and
skipping over the auto-inserted dash — the AppKit equivalent of the Windows
`SelectionStart` and Android `TextFieldValue` join fields. There is **no
protocol "cursor"**; the only cursor logic in the codebase is this caret
handling.

## Prefix = the root secret

The 5-char code is the sole IKM for every key. All derivations are
HKDF-SHA256. Canonical: `TunnelKeyDerivation.cs:18-53` (static) and
`:103-125` (per-minute); macOS twin: `TunnelKeyDerivation.swift:34-68`.

| Output | Salt | Info label | Length | Role |
|---|---|---|---|---|
| `tunnel_server_id` | empty | `bluesteal/tunnel-server-id` | 2 B | picks tunnel host — **clamped to 0 (Google) for v1** |
| `tunnel_id` (legacy) | empty | `bluesteal/tunnel-id` | 16 B | superseded by the per-minute form |
| `tunnel_id_m` (active) | `int64-LE(minute)` | `bluesteal/tunnel-minute` | 16 B | the rotating bridging slot in the WS URL |
| `psk` | empty | `bluesteal/psk` | 32 B | Noise NKpsk0 pre-shared key |
| `responder-static` seed | empty | `bluesteal/responder-static` | 32 B | → P-256 keypair; the NKpsk0 responder `s` |

Each maps onto a real caBLE field (see the table in
[`TUNNEL_SIDE_CHANNEL.md`](TUNNEL_SIDE_CHANNEL.md) — there the `qr_secret` is
the IKM; here the 5-char code is). Because both sides derive `tunnel_id_m`
from `(code, minute)` independently, the minute-boundary hop needs no extra
exchange — that's what makes the rotation work.

### Worked example (KAT, code `BSTL1`)

From `bluesteal-macos/tools/TunnelProbe/KAT.md` (vectors generated from the
canonical C# crypto; the Swift port reproduces them byte-for-byte):

```
psk             = E4BCC2256F15E1E990541F50E86350161364AD9F221C6CF98697C156E12C0843
responder_seed  = D47890BCF7A16A88CA7CAD6259297631C3C175FA47B06939A73FDBF793FD74C1
tunnel_id (legacy, empty salt) = 377B0B4170FBAD1FEFEFFE0BAF6E3402
```

Strip the code and there is no PSK, no responder identity, and no derivable
`tunnel_id` — the Noise handshake can't run and neither side can compute the
URL to open. The prefix is load-bearing, not redundant.

## Suffix = the routing handle

The `routing_id` is **assigned by the tunnel server**, returned in the
`X-caBLE-Routing-ID` response header when the host opens
`wss://<host>/cable/new/<tunnel_id>` (`TunnelHostSocket.swift:17-43`). The
peer must place it in its own connect URL:

```
wss://<host>/cable/connect/<routing_id>/<tunnel_id>
```

It is **not derivable and not predictable**. This was settled empirically —
see [`TUNNEL_SIDE_CHANNEL.md`](TUNNEL_SIDE_CHANNEL.md), *Step 1* and *Probe B*:

- Three `/cable/new/<tid>` opens returned **three distinct** routing_ids →
  it is not a function of `tunnel_id`, so it can't be hashed offline.
- The Chromium-style `routing=000000` fallback **fails** with
  `421 Misdirected Request` → the peer genuinely must learn the host's real
  routing_id out-of-band.

That out-of-band channel is the same one already carrying the code, so the
routing_id rides along as the token suffix. (In real FIDO caBLE this value
travels over the BLE advert instead.)

> Note: the architectural-mapping table in `TUNNEL_SIDE_CHANNEL.md` still
> shows the peer using `000000`. That was the pre-probe assumption and is
> superseded by the Step 1 findings below it — the suffix is mandatory.

## Why neither half can be stripped

- **No prefix** → no `psk` / responder key / `tunnel_id`. Key derivation and
  the Noise handshake collapse entirely. Pairing is impossible.
- **No suffix** → the server can't route the peer's `/cable/connect` to the
  host's publisher socket. The two endpoints never meet.

## Cross-platform invariant

The derivation is **byte-for-byte identical** across Windows (canonical),
macOS, iOS, and Android. The 5-char alphabet (`ABCDEFGHJKMNPQRSTUVWXYZ` — no
I/L/O) and the four HKDF info-label strings above must match **exactly** on
every platform, or pairing fails silently with no obvious error. When adding
a fifth port, copy the labels verbatim and check them against the KAT.
