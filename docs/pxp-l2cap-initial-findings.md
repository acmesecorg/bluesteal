# Initial findings: PXP 1.0 (WD, 2026-07-17) and the proximity-relay class

*Working notes — does the FIDO Proximity Exchange Protocol, and its new direct
Bluetooth L2CAP data channel, address the proximity-relay finding documented in
`caBLE-proximity-relay-disclosure.md`?*

| | |
|---|---|
| **Status** | Initial findings, not yet reviewed. Draft for a later definitive branch. |
| **Source** | FIDO Alliance, *Proximity Exchange Protocol* v1.0 Working Draft, 2026-07-17. |
| **Spec URL** | <https://fidoalliance.org/specs/hybrid/proximity-exchange-protocol-v1.0-wd-20260717.html> |
| **Scope** | Transport (§6), proximity model (§5.4), handshake (§6.2), security considerations (§10). |
| **Relation to prior work** | Extends the caBLE/hybrid proximity-relay disclosure to the standalone PXP spec. |

> **Caveat.** PXP is a Working Draft and states it is "not intended to be a basis
> for any implementations as the Specification may change." These are preliminary
> notes against a moving target; re-verify against the published version before
> relying on them.

---

## 1. Finding in one line

PXP 1.0 WD does **not** address the proximity-relay class. The new direct
Bluetooth **L2CAP** data channel is introduced as an *alternative transport for
reliability*, not as a proximity defense, and it carries **no** round-trip-time,
distance-bounding, signal-strength, or link-binding measure. The relay described
in the caBLE disclosure applies to PXP essentially unchanged.

---

## 2. What PXP's proximity model is

The entire proof of proximity is possession of the BLE advertisement, folded
into the session key. There is no timing or distance measurement anywhere in the
flow.

- §5.4 states the model and its own threat assumption:
  > "PXP requires a proof of proximity to help prevent attacks… By having the
  > [authenticator] demand that the client platform prove reception of a BLE
  > advert such an attacker would have to have control of a Bluetooth radio near
  > to the victim."
- §5.4.1 identifies the proof itself:
  > "The connection nonce is the value that demonstrates possession of the BLE
  > advert, and thus proximity to the CMHD."
- §6.2 binds that nonce into the Noise `KNpsk0` handshake PSK:
  > "The client platform speaks first to prove possession of the BLE advert… the
  > pre-shared symmetric key is derived from the QR secret and decrypted BLE
  > advert."

The decrypted advert is 16 bytes: a flags byte, an 80-bit connection nonce, a
24-bit routing ID, and a 16-bit tunnel-service identifier (§5.4.1). The nonce is
a replayable byte string; nothing binds it to the emitting radio, a location, or
a measured round trip. This is exactly the defect set out in the disclosure
§4.1, now confirmed to be the whole of PXP's proximity proof.

§10 Security Considerations adds no relay analysis — it is a single line
referencing the external FIDO Security Reference document.

---

## 3. What the new L2CAP channel actually is

The L2CAP Connection-oriented Channel (CoC) is an alternative data-transfer
transport, chosen for environments without network connectivity — not a
proximity hardening measure.

- §6.1 (purpose and fallback):
  > "Additional channels in PXP are provided to help improve reliability in
  > certain environments, such as ones without a network connection… The
  > WebSocket data transfer channel SHOULD be supported by both client platform
  > and CMHD for fallback and backwards compatibility."
- §6.1.2 (setup and framing):
  > "the CMHD may choose to create an **insecure** L2CAP Connection-oriented
  > Channel (CoC) Bluetooth server socket… A PSM value (henceforth called
  > *server psm*) uniquely identifying this channel is auto-generated. The CMHD
  > then adds the *server psm* into the advertisement suffix… Every message
  > transmitted over this socket MUST be prefixed with a 4-byte big-endian length
  > prefix… Bluetooth Low Energy L2CAP and extended advertising are optional
  > features… Implementations SHOULD always include websockets as a fallback."

Channel signalling: the advertisement suffix is a CBOR map; the BLE
`transport_channel_identifier` is `1` and `channel_extra` is a CBOR integer
carrying the server PSM (§6.1.2). The channel is explicitly **"insecure"** at the
Bluetooth layer — all confidentiality/authentication comes from the Noise layer
on top, the same `KNpsk0` handshake used over WebSockets.

---

## 4. Why the relay survives on PXP

The disclosure's findings map onto PXP point for point:

1. **Proximity proof is replayable (disclosure §4.1–4.2).** The proof is
   possession of the advert nonce, carried into the PSK. Capturing and
   re-broadcasting the advert gives a remote client exactly what §6.2 requires.
   The cryptographic binding to the advert does not prevent relay — it only
   requires that the advert be forwarded, which is what the PoC does.

2. **No distance is measured.** Neither the WebSocket nor the L2CAP path
   includes a round-trip-time step, latency bound, or distance-bounding
   exchange. A relay adds latency, but nothing checks latency.

3. **The L2CAP connection is itself relayable (MITM).** Because the CoC is
   "insecure" with a simple 4-byte length-prefix framing and Noise end-to-end, a
   relay interposed between the authenticator's L2CAP socket and the remote
   client forwards the length-prefixed frames transparently — it shuttles Noise
   ciphertext and the handshake completes end-to-end, unaware of the hop. The
   spec places no link-layer authentication that would detect interposition.

4. **Downgrade to the tunnel is trivial and spec-blessed.** The authenticator
   offers L2CAP only if the client platform advertised BLE support in the QR, and
   WebSocket is the mandatory-to-support fallback (§6.1). An initiator that
   controls the QR (the attacker's own client in disclosure Scenario A) simply
   does not advertise BLE, forcing the WebSocket path — where disclosure §4.3
   (FIDO's public tunnel servers carry the relay for free) applies unchanged. The
   assigned tunnel domains remain `cable.ua5v.com` and `cable.auth.com` (§6.1.1).

Net: at most, a session that genuinely ran end-to-end over direct L2CAP with no
relay would deny the attacker FIDO's tunnel as a free carrier *for that session*
— but the attacker is never forced into that case, and even within it the CoC is
relayable over the attacker's own link.

---

## 5. Assessment table

| Disclosure finding | PXP 1.0 WD outcome |
|---|---|
| §4.1 advert/exchange not bound to distance | **Not addressed** — proximity proof is advert-nonce-in-PSK; no timing/distance |
| §4.2 capture-and-relay of the advert works | **Not addressed** — relaying the advert satisfies §6.2 |
| §4.3 FIDO tunnel is a free relay carrier | **Not addressed** — WebSocket path retained as mandatory fallback; downgrade available |
| New: direct L2CAP data channel | **Not a defense** — "insecure" CoC, framed as reliability transport, itself MITM-relayable |

---

## 6. Relevance to Recommendation §7 of the disclosure

The disclosure's structural recommendation was to "bind the session to the
Bluetooth link, not only to a one-shot beacon… carry part of the exchange
directly over the BLE channel, **and/or** apply distance-bounding or
round-trip-time constraints." PXP adopts the first clause (data can now flow over
BLE/L2CAP) but **not** the load-bearing second clause (distance bounding / RTT).
Carrying data over a relayable channel, without a distance measurement, does not
make proximity load-bearing.

Open caveat worth testing: even if a future revision adds an RTT bound, BLE
timing is coarse (connection-interval quantization, controller buffering,
~1.25 ms granularity), so a tight, trustworthy distance bound over BLE is itself
hard to enforce.

---

## 7. Open items for the definitive branch

- Confirm quotes against the published (non-WD) version when available.
- Empirically characterize added latency of (a) a direct-L2CAP MITM and (b) a
  WebSocket-path relay, to document what an RTT bound *would* need to reject.
- Check platform behaviour: which of iOS / Android / Windows / macOS actually
  implement the L2CAP path vs. WebSocket-only, and whether any advertise BLE
  support by default (affecting downgrade feasibility).
- Decide whether these findings fold into the existing disclosure as a new
  subsection under §6, or stand as a companion PXP-specific note.

---

## 8. References

- FIDO Alliance. *Proximity Exchange Protocol* v1.0 WD, 2026-07-17. §5.4, §5.4.1,
  §6, §6.1, §6.1.1, §6.1.2, §6.2, §10.
  <https://fidoalliance.org/specs/hybrid/proximity-exchange-protocol-v1.0-wd-20260717.html>
- Companion: `docs/caBLE-proximity-relay-disclosure.md` (this repository).
