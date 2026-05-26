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
| [`bluesteal-windows`](../bluesteal-windows) | C# / WinUI 3 / SIPSorcery | Canonical port — the protocol reference. |
| [`bluesteal-macos`](../bluesteal-macos) | Swift / SwiftUI / WebRTC.framework | Mac desktop client. |
| [`bluesteal-ios`](../bluesteal-ios) | Swift / SwiftUI / WebRTC.framework | iOS client (scan + relay; iOS can't re-broadcast). |
| [`bluesteal-android`](../bluesteal-android) | Kotlin / Compose / io.github.webrtc-sdk | Android client (full scan + re-broadcast). |

All four speak the same wire protocol: WebRTC data channel between paired
devices, with signalling over a public MQTT broker. See
[`specs/PORTING_GUIDE.md`](specs/PORTING_GUIDE.md) for the protocol in detail.

---

## Architecture in one paragraph

Devices pair by exchanging a 5-letter code over the user's voice/eyeball
("read this to your phone"). The code seeds an FNV-1a hash that picks the
same MQTT broker on both ends (HiveMQ / EMQX / Mosquitto are tried in a
fixed lock-step order). Both sides subscribe to topics under
`bluesteal/v1/<code>/{offer,answer,ice/host,ice/peer,bye}` and complete a
standard WebRTC SDP + ICE handshake there. Once the data channel opens, MQTT
is disconnected and adverts flow over the channel directly, with a 4-second
bidirectional heartbeat to keep NAT mappings warm. Either side can
re-broadcast received adverts over its own BLE radio (on platforms that
allow it) so a third device watching the air sees them.

---

## TURN relay configuration

WebRTC tries three kinds of candidate pairs in order: **host** (same LAN),
**srflx** (NAT-reflexive via STUN), and **relay** (TURN server). Same-LAN
pairings work on host candidates and never need TURN. **Cross-AP** pairings
(e.g. two devices on the same WiFi system but reaching different APs via a
mesh / extender) and **cellular ↔ home** pairings frequently can't form
host or srflx pairs, and need TURN.

bluesteal needs a TURN provider with **valid credentials** to make those
cases work. STUN-only is a fine starting point if you only ever pair on the
same LAN — leave the credentials blank and the apps will skip the TURN
entries silently.

### Sign up for Cloudflare Realtime TURN (one-time, free tier)

1. Go to [dash.cloudflare.com](https://dash.cloudflare.com/) and create an
   account if you don't have one. Free tier — no card required.
2. From the dashboard, open **Realtime** in the sidebar → **TURN Service**
   → **Create a TURN key**. Name it something like `bluesteal-dev`.
3. Cloudflare gives you two long-lived values. Save both — they're the
   parent key, not the credentials you paste into apps:
   - `TURN_KEY_ID`
   - `TURN_KEY_API_TOKEN`
4. Mint a *credential pair*. Cloudflare caps the TTL at **172 800 s
   (2 days)** despite the docs implying longer is allowed — so manual
   rotation is part of life on the free tier:

   ```bash
   TURN_KEY_ID=...        # from step 3
   TURN_KEY_API_TOKEN=... # from step 3
   curl -s https://rtc.live.cloudflare.com/v1/turn/keys/$TURN_KEY_ID/credentials/generate-ice-servers \
     -H "Authorization: Bearer $TURN_KEY_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"ttl": 172800}' \
     | jq -r '.iceServers[] | select(.username) | "username=\(.username)\ncredential=\(.credential)"'
   ```

   That prints the two strings the apps want.

### Paste credentials into each platform

Every repo has a **`Secrets.{swift,cs}.template`** committed with empty
defaults plus a `.gitignore` rule for the real `Secrets.{swift,cs}` next to
it. The `.template` suffix matters: Xcode 16's synchronized file groups and
SDK-style `.csproj` globbing would otherwise compile both files and produce
a duplicate-declaration error. On each fresh clone, copy the template once
before first build:

#### Android (`bluesteal-android`)

`local.properties` is gitignored by default. Copy the example and fill in:

```bash
cd bluesteal-android
cp local.properties.example local.properties
# edit local.properties — set TURN_USERNAME and TURN_CREDENTIAL
```

The values get baked into `BuildConfig` at build time via
`app/build.gradle.kts`, and `RelayClient.kt` reads them. Blank values are
tolerated — falls back to STUN-only.

#### macOS (`bluesteal-macos`)

```bash
cd bluesteal-macos
cp Secrets.swift.template bluesteal/Secrets.swift
# edit bluesteal/Secrets.swift — set turnUsername and turnCredential
```

No Xcode UI step needed — this project uses Xcode 16 synchronized file
groups, so any `.swift` inside `bluesteal/` is picked up automatically on
the next build. The file content is gitignored.

#### iOS (`bluesteal-ios`)

Same shape as macOS:

```bash
cd bluesteal-ios
cp Secrets.swift.template bluesteal-ios/Secrets.swift
# edit bluesteal-ios/Secrets.swift
```

Same synchronized-group story as macOS — no Xcode UI step.

#### Windows (`bluesteal-windows`)

```bash
cd bluesteal-windows
cp Bluesteal/Bluesteal/Secrets.cs.template Bluesteal/Bluesteal/Secrets.cs
# edit Bluesteal/Bluesteal/Secrets.cs — set TurnUsername and TurnCredential
```

The `.csproj` uses SDK-style globbing, so the new `Secrets.cs` is picked
up automatically on the next build — no project-file edit needed.

### Rotating credentials

Cloudflare caps the TTL at 2 days. When ICE starts failing on non-LAN
pairings, re-run the curl from step 4 above and paste the new values into
the same four files. Old credentials silently stop being accepted by the
TURN server — symptom: `ice.connectionState = FAILED` on roams that
previously worked via relay.

For convenience, this project also lets you keep a gitignored
`TURN_ROTATION.local.md` in the root repo with your actual `TURN_KEY_ID` and
`TURN_KEY_API_TOKEN` inline alongside a one-shot bash that patches all four
files. That file is excluded by `.gitignore` — never commit it.

### Hosting your own TURN

If you'd rather not depend on Cloudflare, the same secrets files take any
TURN provider's credentials. The URL list inside each platform's
`RelayClient` is the place to change the server hostname / ports. For a
self-hosted [coturn](https://github.com/coturn/coturn) on a small VPS, two
URLs is usually enough:

```
turn:your-turn.example.com:3478?transport=udp
turns:your-turn.example.com:5349?transport=tcp
```

---

## Building each platform

### macOS / iOS (Xcode)

```bash
git clone https://github.com/<owner>/bluesteal-macos.git
# fill in Secrets.swift (see above)
open bluesteal-macos/bluesteal.xcodeproj
```

Same for iOS: clone `bluesteal-ios`, fill in `Secrets.swift`, open the
`.xcodeproj`.

### Android (Android Studio or CLI)

```bash
git clone https://github.com/<owner>/bluesteal-android.git
cd bluesteal-android
cp local.properties.example local.properties
# fill in local.properties with sdk.dir + TURN_USERNAME + TURN_CREDENTIAL
./gradlew installDebug   # with an Android device or emulator attached
```

Open in Android Studio if you prefer a GUI.

### Windows (Visual Studio 2022)

```bash
git clone https://github.com/<owner>/bluesteal-windows.git
# edit Bluesteal/Bluesteal/Secrets.cs
# open Bluesteal/Bluesteal.sln in Visual Studio and run
```

Requires the WinUI 3 / Windows App SDK workload.

---

## Project docs (`specs/`)

| File | What it covers |
|------|----------------|
| [`PORTING_GUIDE.md`](specs/PORTING_GUIDE.md) | Wire protocol, broker selection, signalling topics, ICE handling. Read this first if you're writing a fifth port. |
| [`MACOS_RELAY_PAIRING_FIXES.md`](specs/MACOS_RELAY_PAIRING_FIXES.md) | The "things to get right" list when adopting the relay code on a new platform — broker lock-step, candidate buffering, ICE-failed teardown, etc. |
| [`RELAY_DEBUGGING.md`](specs/RELAY_DEBUGGING.md) | Per-PID JSONL tracer layout and how to read two trace files side-by-side when diagnosing a failed pairing. |
| [`REBROADCAST_PLAN.md`](specs/REBROADCAST_PLAN.md) | Design notes for the "re-emit received adverts over the local BLE radio" feature. |
| [`MACOS_UPDATES.md`](specs/MACOS_UPDATES.md) | Historical changes specific to the macOS port. |
| [`IOS_AUTHENTICATOR_CAPTURE.md`](specs/IOS_AUTHENTICATOR_CAPTURE.md) | Notes on capturing hybrid-FIDO traffic on iOS for protocol research. |

---

## Status

Personal-scale research project. Pairing works cross-platform; the
re-broadcast path is supported everywhere except iOS (Core Bluetooth peripheral
mode rejects raw service-data for third-party apps).

Issues / PRs welcome — please don't commit your TURN credentials.
