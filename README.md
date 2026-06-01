# bluesteal

A small experiment in observing and re-broadcasting **FIDO CTAP 2.2 hybrid-tunnel
("caBLE") Bluetooth Low Energy advertisements** across devices.

Two paired devices show each other the caBLE adverts they are scanning live —
adverts the FIDO authenticator and Chromium-flavoured caller emit during a
hybrid sign-in flow. One side can also re-broadcast a remote peer's adverts
locally over its own BLE radio, so a CTAP exchange that started on one phone
can be observed (and on platforms that allow it, completed) from another.

Originally built as a debugging aid for hybrid-FIDO interop work.

This umbrella repo holds the **shared specs and docs**. The four platform
implementations live in their own repos alongside this one:

| Repo | Stack | Role |
|------|-------|------|
| [`bluesteal-windows`](https://github.com/acmesecorg/bluesteal-windows) | C# / WinUI 3 | Canonical port — the protocol reference. Dual-role + re-broadcast. |
| [`bluesteal-macos`](https://github.com/acmesecorg/bluesteal-macos) | Swift / SwiftUI | Mac desktop client. Dual-role + re-broadcast. |
| [`bluesteal-ios`](https://github.com/acmesecorg/bluesteal-ios) | Swift / SwiftUI | iOS client. **Host-only** (scan + host; iOS can't join or re-broadcast). |
| [`bluesteal-android`](https://github.com/acmesecorg/bluesteal-android) | Kotlin / Jetpack Compose | Android client. Dual-role + faithful re-broadcast (all variants). |

All four speak the same wire protocol over a single transport: a **caBLE
tunnel** (see below). The cryptography is byte-compatible across .NET, CryptoKit
(Apple) and BouncyCastle (Android), so any platform can pair with any other.

---

## Architecture in one paragraph

Devices pair over the public **caBLE tunnel servers** (`cable.ua5v.com` et al.)
— the same WebSocket bridges Chromium and FIDO authenticators use for hybrid
sign-in, here carrying bluesteal's own framed messages instead of CTAP traffic.
The host derives a tunnel slot from a short pairing code (HKDF-SHA256 →
`tunnel_id`, pre-shared key, and a responder static key), opens
`wss://<host>/cable/new/<tunnel_id>`, and learns its server-assigned 3-byte
`routing_id`. It surfaces an **11-character out-of-band token** —
`CODE-ROUTING`, e.g. `ABCDE-7C4A9C` — which the user shares the same way they'd
share the code (verbally / iMessage / WhatsApp). The peer connects to
`wss://<host>/cable/connect/<routing_id>/<tunnel_id>`; the two run a **Noise
NKpsk0** handshake (used purely as an AEAD framing layer, *not* FIDO-grade
authentication) and then exchange `RelayMessage` JSON. Because the relay caps
idle sockets at ~60 s, the host runs a **make-before-break hop every 30 s** —
opening a fresh tunnel and promoting the peer onto it without dropping the
session — plus a ~2 s heartbeat to keep traffic flowing. Either side (except
iOS) can re-broadcast received adverts over its own BLE radio so a third device
watching the air sees them.

See [`specs/TUNNEL_SIDE_CHANNEL.md`](specs/TUNNEL_SIDE_CHANNEL.md) for the full
derivation, the OOB-token rationale, and the empirical server-bridging results.

> **Off-label use.** The caBLE tunnel servers are operated by Google/Apple for
> real FIDO traffic. Using them as a generic byte bridge works because the
> server can't see inside the Noise channel, but it is unsupported and could be
> rate-limited or blocked at any time. Two sessions sharing a code collide on
> `tunnel_id` (they can't observe each other — the bridge is 1:1). This is a
> personal-scale research tool, not a product.

---

## Building each platform

No secrets or credentials are needed — just clone and build.

### macOS / iOS (Xcode)

```bash
git clone https://github.com/acmesecorg/bluesteal-macos.git
open bluesteal-macos/bluesteal.xcodeproj      # scheme: bluesteal
```

Same for iOS: clone `bluesteal-ios`, open the `.xcodeproj` (scheme
`bluesteal-ios`). Both use Xcode 16 file-system-synchronized groups, so any
source file under the app folder is picked up automatically. iOS needs a signing
team for device builds (`-allowProvisioningUpdates`).

### Android (Android Studio or CLI)

```bash
git clone https://github.com/acmesecorg/bluesteal-android.git
cd bluesteal-android
# AGP needs JDK 17+. If your default `java` is older, point at Android Studio's JBR:
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
./gradlew installDebug                          # with a device/emulator attached
```

Open in Android Studio if you prefer a GUI.

### Windows (Visual Studio 2022)

```bash
git clone https://github.com/acmesecorg/bluesteal-windows.git
# open Bluesteal/Bluesteal.sln in Visual Studio and run
```

Requires the WinUI 3 / Windows App SDK workload.

---

## Using it

1. On one device, click **Host**. It shows an 11-character token
   (`CODE-ROUTING`). Copy it.
2. Share the token with the second device (out of band — read it aloud, message
   it, etc.).
3. On the second device, click **Join…**, paste the token, **Connect**.
4. Within ~30–60 s the tunnel reaches **open** on both ends and live adverts
   flow. Join promptly — the host's first tunnel socket ages out at the relay's
   idle cap before the first hop engages.

iOS is host-only: it always hosts, and a macOS/Windows peer joins its token.

### Driving host/join for testing

Each app can be auto-driven without tapping the UI; the hosted token is written
to the trace log as a `token.ready` event.

| Platform | Host | Join |
|----------|------|------|
| **macOS** | `open -n Bluesteal.app --args -host` | `open -n Bluesteal.app --args -join <TOKEN>` |
| **Windows** | launch with `-host` (see the `Host` launch profile) | launch with `-join <TOKEN>` |
| **Android** | `adb shell am start -n org.crainiate.bluesteal/.ui.MainActivity --ez host true` | `… --es join <TOKEN>` |
| **iOS** | `bluesteal://host` URL (tap / Shortcut / Safari); simulator: `xcrun simctl launch <udid> org.crainiate.bluesteal-ios -host` | host-only — no join |

---

## Re-broadcast support

When a session is open, a device can re-emit the adverts it receives from its
peer over its own BLE peripheral, so a nearby CTAP hybrid client (e.g. Chrome on
a laptop) can observe them. Fidelity varies by platform's BLE peripheral API:

- **Windows / Android** — emit raw service-data and manufacturer-data, so every
  advert variant is re-broadcast faithfully (HMAC tag intact).
- **macOS** — `CBPeripheralManager` only accepts service-UUIDs from third-party
  apps, so the EID is packed into a 128-bit service UUID (the Chromium `IS_MAC`
  convention); a lossy but Chrome-on-macOS-compatible path.
- **iOS** — cannot re-broadcast at all (Core Bluetooth peripheral mode rejects
  raw service-data for third-party apps), hence host-only.

See [`specs/REBROADCAST_PLAN.md`](specs/REBROADCAST_PLAN.md) for the design and
[`specs/IOS_AUTHENTICATOR_CAPTURE.md`](specs/IOS_AUTHENTICATOR_CAPTURE.md) for
the advert-capture work that informed it.

---

## Project docs (`specs/`)

| File | What it covers |
|------|----------------|
| [`TUNNEL_SIDE_CHANNEL.md`](specs/TUNNEL_SIDE_CHANNEL.md) | The caBLE tunnel transport: key derivation from the pairing code, the 11-char OOB token, make-before-break hops, server-bridging probe results, off-label-use risk. Read this first to understand the protocol. |
| [`REBROADCAST_PLAN.md`](specs/REBROADCAST_PLAN.md) | Design of the "re-emit received adverts over the local BLE radio" feature, including the per-platform peripheral-API limits. |
| [`IOS_AUTHENTICATOR_CAPTURE.md`](specs/IOS_AUTHENTICATOR_CAPTURE.md) | How to capture a native iOS authenticator's hybrid-transport advert (using the scanner's Diagnostic mode) and what wire format it uses. |

---

## Status

Personal-scale research project. Pairing works cross-platform over the caBLE
tunnel; the re-broadcast path is supported everywhere except iOS.
