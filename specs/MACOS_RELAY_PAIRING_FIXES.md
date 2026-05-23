# Mobile / macOS / Android Relay Pairing Fixes

Fixes the Windows port applied while getting the WebRTC pairing handshake to
work reliably between two `bluesteal` instances. The iOS client
(`bluesteal-ios`), macOS client (`bluesteal-macos` aka `bluemonday`), and
Android client (`bluesteal-android`) share the same architecture
(`SignalingClient` + `RelayClient`) and should adopt these same fixes for
robust same-platform and cross-platform pairing.

All log/event names below refer to the per-PID JSONL tracer added on Windows
(`%LOCALAPPDATA%\bluesteal\Logs\relay-{date}-{pid}.jsonl`). The Apple ports
write equivalent JSONL to `~/Library/Application Support/bluesteal/Logs` (or
the sandbox container path under `~/Library/Containers/org.crainiate.bluesteal/`);
Android writes to `/storage/emulated/0/Android/data/org.crainiate.bluesteal/files/Logs/`
(pull via `adb pull`). The shape of every record is identical so the four
platforms can be diffed line-for-line during debugging — except for the
Android-specific deviations called out in **Section 11**.

---

## 1. Deterministic broker selection (FNV-1a hash of pairing code)

**Problem:** Host and Peer independently picked their starting MQTT broker
(round-robin / first-available). On flaky networks they landed on *different*
brokers — e.g. Host on HiveMQ, Peer on EMQX — and the handshake never
converged. No log line ever indicated the mismatch.

**Fix (Windows `SignalingClient.cs` → `StartingBrokerIndex()`):** Hash the
5-character pairing code with FNV-1a, modulo broker count, to pick a starting
index. Both sides use the same code, so both compute the same start. Fallback
walks the list in the same order on both sides.

```swift
// bluesteal-macos/Sources/Services/SignalingClient.swift
private func startingBrokerIndex() -> Int {
	var hash: UInt32 = 2166136261
	for byte in code.utf8 {
		hash ^= UInt32(byte)
		hash &*= 16777619
	}
	return Int(hash % UInt32(brokers.count))
}
```

**Broker list must match Windows exactly and in the same order:**

| Index | Host | Port | Path | TLS |
|-------|------|------|------|-----|
| 0 | `broker.hivemq.com` | 8884 | `/mqtt` | yes (WSS) |
| 1 | `broker.emqx.io` | 8084 | `/mqtt` | yes (WSS) |
| 2 | `test.mosquitto.org` | 8081 | `/mqtt` | yes (WSS) |

If macOS reorders or substitutes brokers, hashing converges on a *different*
index from Windows and cross-platform pairing breaks again.

---

## 2. Per-broker connect timeout (6 seconds)

**Problem:** A slow / hung broker could block the handshake indefinitely. On
this network `test.mosquitto.org` reliably hangs for >30 s before failing.

**Fix:** Wrap each broker's connect in a 6 s cancellation. On timeout, dispose
the in-flight client and try the next broker.

```swift
// Pseudocode
let timeoutTask = Task { try await Task.sleep(nanoseconds: 6_000_000_000); throw CancellationError() }
let connectTask = Task { try await client.connect(...) }
// race them; on timeout, cancel connectTask and tear down client before retry
```

Log `mqtt.connect.attempt`, `mqtt.connect.timeout`, `mqtt.connect.failed`,
`mqtt.connect.connected`, `mqtt.connect.exhausted` so the trace shows exactly
which broker won.

Observed timings on Windows: HiveMQ inconsistent, EMQX ~500 ms reliably,
Mosquitto always hits the 6 s timeout. Without the timeout the user waits
30+ s for the WSS connect to fail naturally.

**Android exception:** Android uses a **10 s** per-broker budget, not 6 s.
The HiveMQ Java client + Netty take longer for the first TLS + MQTT CONNECT
on a cold JVM, and a 6 s budget kills the connection mid-handshake even on
networks where macOS / iOS connect to the same broker in ~500 ms. The
deterministic FNV-1a-seeded broker walk still works in lock-step — a longer
Android budget just means it converges to the same reachable broker a beat
later, not differently. See **Section 11** for the full Android deviations
(IPv4 pre-resolve, plain MQTT-over-TLS instead of WSS).

---

## 3. Cancellable session — cancel in-flight connect on `Disconnect()`

**Problem:** Tapping Cancel/Disconnect tore down the `RTCPeerConnection` and
`SignalingClient`, but an already-issued broker connect kept running. When it
eventually completed it ran callbacks against the now-null peer connection,
producing `NullReferenceException` (Windows) / crashes (macOS likely
equivalent under ARC).

**Fix (`RelayClient.cs`):**
- Hold a `_sessionCts: CancellationTokenSource`. Cancel it in `Disconnect()`.
- Pass its token into `signaling.ConnectAsync(ct)`.
- In `OnSignalingConnected`, early-return if `_pc == null`, `_signaling == null`,
  or `State == .closed`. Log this as `relay.signaling.connected.ignored`.

macOS equivalent: a `Task` handle stored on the relay client, cancelled in
`disconnect()`; the `signalingDidConnect` delegate must guard against the
session having been torn down.

---

## 4. Thread-safe `SignalingClient` disposal (`Interlocked.Exchange`)

**Problem:** Concurrent calls to `DisconnectAsync` and `Dispose` (e.g. the
broker disconnect callback firing while the UI also calls disconnect) raced
on the `_client` field — one path read it, the other nulled and disposed it,
the first then dereferenced null.

**Fix:** Atomically take ownership of the field before touching it.

```csharp
var local = Interlocked.Exchange(ref _client, null);
local?.Dispose();
```

macOS equivalent: serialise teardown on a dedicated actor / `DispatchQueue`,
or use an `os_unfair_lock` to swap the optional client to `nil` before
disconnecting it. Whatever the mechanism, only **one** path may ever call
`disconnect()`/`dealloc` on the underlying MQTT client.

---

## 5. `ondatachannel` may surface an already-open channel

**Problem:** On the Peer, SIPSorcery delivered the incoming `RTCDataChannel`
with `readyState == open` *before* we'd attached an `onopen` handler. The
handler never fired, the Peer stayed in `Negotiating`, and pairing silently
hung after ICE/DTLS were already up.

**Fix (`RelayClient.WireDataChannel`):** After wiring `onopen`, synchronously
check `channel.readyState`; if already open, invoke the handler immediately.
Log this as `dc.alreadyOpen` so it shows in the trace.

```swift
// macOS — apply the same guard in RTCPeerConnectionDelegate
func peerConnection(_ pc: RTCPeerConnection, didOpen dc: RTCDataChannel) {
	wire(dc)
	if dc.readyState == .open { handleOpen(dc) } // catch the race
}
```

Apple's WebRTC binding has the same property — verify on macOS by logging the
`readyState` at the moment `didOpen` fires.

---

## 6. Trace every `setRemoteDescription` result and signaling parse

**Problem:** Non-OK results from `setRemoteDescription`, malformed signaling
payloads, and "unexpected" signaling types were being swallowed. Diagnosis
required adding a print for every branch.

**Fix:** Log:
- `relay.setRemoteDescription` with `{ result }` on every call (success and
  failure).
- `signaling.unexpected` on any message type that doesn't match the role's
  expected set.
- `signaling.parse.error` on JSON decode failure with the raw payload preview
  (truncated to ~200 chars).
- `signaling.receive.filtered` when filtering out own-origin retained
  messages.

On the Host, retained `offer` messages on the broker cause the Host to
receive its own offer back; without `signaling.receive.filtered` this looks
like a peer that immediately replies with the offer instead of an answer.

---

## 7. Data-channel send/receive tracing

**Problem:** After pairing reached `Open`, no adverts crossed and ICE failed
at +30 s. Without `send`/`onmessage` tracing it was impossible to tell
whether: (a) heartbeat loop never fired, (b) `send()` threw silently,
(c) packets were dropped on the wire.

**Fix:** Wrap every send and receive:

```swift
func send(_ message: RelayMessage) {
	guard let dc = dc else { log("dc.send.skipped", ["reason": "no channel"]); return }
	guard dc.readyState == .open else { log("dc.send.skipped", ["state": "\(dc.readyState)"]); return }
	do {
		let json = try JSONEncoder().encode(message)
		let buffer = RTCDataBuffer(data: json, isBinary: false)
		dc.sendData(buffer)
		log("dc.send", ["type": "\(type(of: message))", "bytes": json.count])
	} catch {
		log("dc.send.error", ["error": "\(error)"])
	}
}

// in RTCDataChannelDelegate.didReceiveMessageWith:
log("dc.message", ["bytes": buffer.data.count, "preview": preview(buffer.data)])
```

---

## 8. Per-PID structured trace file (recommended infrastructure)

Windows writes JSONL to `%LOCALAPPDATA%\bluesteal\Logs\relay-{yyyy-MM-dd}-{pid}.jsonl`
with one line per event:

```json
{"ts":"2026-05-18T10:24:01.123Z","pid":42032,"instanceId":"a1b2c3d4","role":"Host","code":"MK9DHTDC","category":"dc","event":"dc.send","data":{"type":"Advert","payloadBytes":312}}
```

Benefits proven during this debugging cycle:
- Two simultaneous instances never interleave (separate files per PID).
- Diffable side-by-side with `Compare-Object` / `diff`.
- The `event` names form a fixed vocabulary, so a "missing event" pinpoints
  exactly which step never ran.

macOS equivalent: write to
`~/Library/Application Support/bluesteal/Logs/relay-{date}-{pid}.jsonl`.
Mirror to `os_log` for live `Console.app` viewing.

### Minimum event vocabulary to mirror

| Category | Events |
|----------|--------|
| `app` | `app.start` |
| `mqtt` | `mqtt.connect.start`, `mqtt.connect.attempt`, `mqtt.connect.connected`, `mqtt.connect.timeout`, `mqtt.connect.failed`, `mqtt.connect.cancelled`, `mqtt.connect.exhausted`, `mqtt.subscribed`, `mqtt.publish`, `mqtt.publish.skipped`, `mqtt.publish.error`, `mqtt.disconnected` |
| `signaling` | `signaling.receive`, `signaling.receive.filtered`, `signaling.receive.empty`, `signaling.parse.error`, `signaling.unexpected` |
| `relay` | `relay.state.change`, `relay.signaling.connected`, `relay.signaling.connected.ignored`, `relay.setRemoteDescription`, `relay.teardown`, `relay.disconnect.user`, `relay.reconnect.begin`, `relay.reconnect.attempt`, `relay.reconnect.success`, `relay.reconnect.error`, `relay.watchdog.silence` |
| `ice` | `ice.gathering.state`, `ice.connection.state`, `ice.local`, `ice.remote`, `ice.grace.start`, `ice.recovered` |
| `dc` | `dc.created`, `dc.alreadyOpen`, `dc.open`, `dc.close`, `dc.send`, `dc.send.skipped`, `dc.send.error`, `dc.message`, `dc.parse.error` |

---

## 9. Cross-platform checks once iOS/macOS/Android adopts the above

After the iOS/macOS/Android fixes land, run one Host on iOS/macOS/Android and
one Peer on Windows (and vice versa) and confirm in the two trace files:

1. Both sides log the **same `startingBrokerIndex`** for the same code.
2. Both sides reach `mqtt.connect.connected` on the **same broker host**.
3. Host logs `relay.setRemoteDescription { result: OK }` after Peer's answer.
4. Both sides log `ice.connection.state = connected` then `dc.open`.
5. **Both directions** log `dc.send` for a `Heartbeat` at ~4 s cadence AND
   the remote side logs the matching `dc.message` with `"type":"heartbeat"`.
   If only one direction is observed, the keepalive bug is still present.
6. On a deliberately-induced network blip (e.g. disable Wi-Fi for 10 s then
   re-enable), both sides log `ice.grace.start` followed by `ice.recovered`
   — session survives without user intervention.
7. On a longer outage (>15 s), both sides log `relay.reconnect.begin`,
   one or more `relay.reconnect.attempt`, then `relay.reconnect.success`
   and `state.change to Open` — full automatic recovery.
8. On user Disconnect, `relay.disconnect.user` is logged and no further
   `relay.reconnect.attempt` events appear.

If any of these are absent, the event name itself identifies the broken step.

For an Android Host paired with a Windows / macOS / iOS Peer (or vice versa)
the broker walk lands them on different transports on the same broker — Android
on plain TLS port `8883`, everyone else on WSS port `8084` / `8884` / `8081`.
This is normal; the broker pub/subs the same topics across transports. See
**Section 11** for the rationale.

---

## File map

| Concern | Windows | iOS | macOS | Android |
|---------|---------|-----|-------|---------|
| Broker selection + per-broker timeout | `Bluesteal/Services/SignalingClient.cs` | `Services/SignalingClient.swift` | `Services/SignalingClient.swift` | `services/SignalingClient.kt` |
| Cancellable session + late-arrival guard + reconnect | `Bluesteal/Services/RelayClient.cs` | `Services/RelayClient.swift` | `Services/RelayClient.swift` | `services/RelayClient.kt` |
| Bidirectional heartbeat + watchdog | `Bluesteal/Services/AdvertRelay.cs` | `Services/AdvertRelay.swift` | `Services/AdvertRelay.swift` | `services/AdvertRelay.kt` |
| `dc.alreadyOpen` race fix | `RelayClient.WireDataChannel` | `RTCPeerConnectionDelegate.didOpen` | `RTCPeerConnectionDelegate.didOpen` | `PeerObserverImpl.onDataChannel` |
| Atomic client disposal | `SignalingClient.DisconnectAsync` / `Dispose` | actor / lock around the MQTT client handle | actor / lock around the MQTT client handle | `AtomicReference<Mqtt3AsyncClient?>` |
| Tracer | `Bluesteal/Services/RelayTracer.cs` | `RelayTracer.swift` → `~/Documents/bluesteal/Logs` | `RelayTracer.swift` → `~/Library/Application Support/bluesteal/Logs` | `logging/RelayTracer.kt` → app-files `Logs/` (pull via `adb`) |
| IPv4 pre-resolve + TLS-MQTT transport | n/a | n/a | n/a | `SignalingClient.resolveIPv4` + `Broker` port `8883` |

---

## 10. Indefinite uptime: bidirectional heartbeat + auto-reconnect

### Symptom

When the iOS/macOS host's BLE scanner goes quiet (no adverts to relay), the
host sends nothing on the data channel. The peer's NAT mapping ages out
(UDP idle timeout 30 s–several minutes depending on router), SIPSorcery's
ICE consent-freshness trips, and the peer logs:

```
ice.connectionState = disconnected  (NAT binding expired)
ice.connectionState = failed        (~9 s later, consent-freshness)
relay.disconnect {reason:"ICE failed"}
```

Root cause: keepalive was one-way (peer→host). Nothing refreshed the host's
outbound NAT mapping.

### Fix 1: Bidirectional 4 s Heartbeat

Both Host and Peer run `AdvertRelay.RunHeartbeatLoop` — a `PeriodicTimer(4s)`
that sends `RelayMessage.Heartbeat` for the entire duration of `RelayState.Open`.

Wire format (unchanged — backwards compatible):
```json
{"type":"heartbeat","payload":{"ts":"2026-05-22T19:51:39Z","knownIds":["id1","id2"]}}
```

**Requirement:** the heartbeat loop MUST run for **both roles** (Host and Peer).
The iOS app was previously only sending heartbeats on the Peer side (or not at
all); this is the primary bug that caused the connection drops.

Swift implementation:
```swift
// AdvertRelay.swift — start when relay.state == .open, for BOTH roles
private static let heartbeatIntervalSeconds: UInt64 = 4

private func startHeartbeats() {
    stopHeartbeats()
    heartbeatTask = Task { [weak self] in
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: heartbeatIntervalSeconds * 1_000_000_000)
            guard let self, self.relay.state == .open else { break }
            let ids = self.store.adverts.map(\.id)
            self.relay.send(.heartbeat(.init(ts: Date(), knownIds: ids)))
        }
    }
}
```

### Fix 2: 5 s ICE-disconnect grace window

When `RTCIceConnectionState` transitions to `.disconnected`, start a 5 s
timer. If state recovers to `.connected`/`.completed` within that window,
cancel the timer and log `ice.recovered`. If the timer expires (or state
moves directly to `.failed`), begin the reconnect cycle.

This prevents single dropped STUN probes from immediately killing the
session.

### Fix 3: 15 s inbound-silence watchdog

Track the timestamp of every inbound data-channel message (heartbeat or
advert). A 2 s ticker checks `(now - lastInbound) >= 15 s`. If tripped,
begin the reconnect cycle even if ICE claims to be connected (catches
half-open paths where consent-freshness is lagging).

### Fix 4: Auto-reconnect with exponential backoff

When a non-user-initiated failure is detected (ICE grace expired, watchdog
tripped, data-channel closed unexpectedly), the session enters a new
`Reconnecting` state:

1. Transport is torn down (`pc`, `dc`, `signaling`), but `code` and `role`
   are preserved.
2. A backoff ladder `[1, 2, 4, 8, 16, 30]` seconds determines wait time
   between attempts.
3. Each attempt re-runs the original `Host()`/`Join()` flow — creates a new
   `RTCPeerConnection`, reconnects signaling (MQTT), and negotiates a new
   WebRTC session on the **same pairing code**.
4. On success (state reaches `Open`), backoff resets to zero.
5. The loop continues **indefinitely** until:
   - The user explicitly calls `disconnect()` (→ `Closed`, publishes `Bye`).
   - A `Bye` message is received from the peer (→ `Closed`).
   - The object is disposed.

Retained MQTT `Offer` ensures a reconnecting peer immediately receives the
host's standing offer without waiting for a fresh publish.

Swift implementation sketch:
```swift
// RelayClient.swift
private static let backoffLadder = [1, 2, 4, 8, 16, 30]

private func beginReconnect(reason: String) {
    guard !userInitiatedClose, state != .reconnecting, state != .closed else { return }
    tracer.log("relay", "relay.reconnect.begin", ["reason": reason])
    tearDownTransport(reason: reason)
    stopWatchdog()
    state = .reconnecting
    reconnectTask = Task { await reconnectLoop() }
}

private func reconnectLoop() async {
    while !Task.isCancelled && !userInitiatedClose {
        let wait = Self.backoffLadder[min(reconnectAttempt, Self.backoffLadder.count - 1)]
        tracer.log("relay", "relay.reconnect.attempt", ["attempt": reconnectAttempt, "waitSeconds": wait])
        try? await Task.sleep(nanoseconds: UInt64(wait) * 1_000_000_000)
        guard !Task.isCancelled else { return }
        reconnectAttempt += 1
        do {
            try await start()
            // Wait for state to reach .open (up to 30 s)
            let deadline = Date().addingTimeInterval(30)
            while state != .open && Date() < deadline && !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            if state == .open {
                tracer.log("relay", "relay.reconnect.success", ["attempts": reconnectAttempt])
                reconnectAttempt = 0
                return
            }
            tearDownTransport(reason: "reconnect attempt timed out")
        } catch {
            tearDownTransport(reason: "reconnect attempt failed: \(error)")
        }
    }
}
```

### ICE state delegate (Swift)

```swift
func peerConnection(_ pc: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
    tracer.log("ice", "ice.connectionState", ["state": "\(newState)"])
    switch newState {
    case .disconnected:
        cancelIceGrace()
        tracer.log("ice", "ice.grace.start", ["graceSeconds": 5])
        iceGraceTask = Task {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled else { return }
            beginReconnect(reason: "ICE disconnected (grace expired)")
        }
    case .connected, .completed:
        if iceGraceTask != nil {
            tracer.log("ice", "ice.recovered")
            cancelIceGrace()
        }
    case .failed:
        cancelIceGrace()
        beginReconnect(reason: "ICE failed")
    default: break
    }
}
```

### Validation matrix

| Scenario | Expected |
|----------|----------|
| Idle >10 min, no BLE adverts | `dc.message {type:heartbeat}` every ~4 s from both sides; no disconnects. |
| Wi-Fi off 8 s, back on | `ice.grace.start` → `ice.recovered`; no reconnect needed. |
| Wi-Fi off 30 s, back on | `relay.reconnect.begin` → `relay.reconnect.attempt` → `relay.reconnect.success` → `state.change to Open`. |
| Kill host process, restart with same code | Peer attempts reconnect indefinitely at 30 s cap; host re-pairs automatically when it comes back. |
| User clicks Disconnect | `relay.disconnect.user`; `Bye` published; state → `Closed`; no further attempts. |

---

## 11. Android-specific deviations (Pixel 5 / Android 14, HiveMQ Java + Netty)

The Android port (`bluesteal-android`) hits two issues that none of Windows /
macOS / iOS see, both rooted in the HiveMQ Java client and its Netty
transport. Both are codified in `services/SignalingClient.kt` and the only
ones to expect when diffing Android traces against the other platforms.

### 11.1 Netty resolves AAAA records first and ignores the JVM property

`io.netty.resolver.dns.DnsNameResolver` doesn't honour
`java.net.preferIPv6Addresses=false` at runtime — even when set in the
`Application.onCreate()` before any networking call. On networks that route
IPv4 to these brokers fine but black-hole IPv6 (every mobile carrier and
most public Wi-Fi we tested), every `connect` attempt either gets an instant
`ECONNREFUSED` against the AAAA address or sits there until the per-broker
budget fires.

Observed in the trace before the fix:

```
mqtt.connect.attempt   host=broker.hivemq.com port=8884
mqtt.connect.failed    error="Connection refused: broker.hivemq.com/[2a01:578:13::37b:6f86]:8884"
```

(IPv6 literal in square brackets is the giveaway.)

**Fix:** resolve the hostname ourselves and filter to the first `Inet4Address`,
then pass HiveMQ a pre-resolved `InetSocketAddress` built with
`InetAddress.getByAddress(host, ipv4Bytes)`. That escape hatch keeps the
hostname attached to the address so:

- The socket opens to the IPv4 IP (Netty can't pick AAAA);
- TLS SNI presents the original hostname (cert validates);
- For TLS-MQTT (see 11.2) there's no `Host:` header to worry about.

```kotlin
private fun resolveIPv4(host: String, port: Int): InetSocketAddress {
    val all = InetAddress.getAllByName(host)
    val v4 = all.firstOrNull { it is Inet4Address }
        ?: throw RuntimeException("No IPv4 address for $host")
    val labelled = InetAddress.getByAddress(host, v4.address)
    return InetSocketAddress(labelled, port)
}
```

Logged as `mqtt.resolve.ok { host, ip, family }` before each
`mqtt.connect.attempt` so the trace makes the IP family explicit. If a future
trace shows `family=IPv6` the resolver hook regressed.

### 11.2 The HiveMQ WSS handshake hangs against every public broker

Even after IPv4 pre-resolve — confirmed at the TCP layer via `adb shell nc`
that the phone can open TCP to all three brokers — `HiveMQ.connectWith().send()`
over the WSS endpoints (`8884` / `8084` / `8081`) sits silently through the
full per-broker budget and produces no error from the client. Same brokers,
same IPs, same phone network: macOS / iOS complete the WSS handshake in
~500 ms.

We didn't fully root-cause whether it's the WebSocket upgrade, TLS protocol
negotiation, or something in HiveMQ's Netty pipeline, because the practical
fix is to bypass WSS entirely.

**Fix:** Android uses plain MQTT-over-TLS on port **`8883`** (the IANA-assigned
MQTT-TLS port) instead of WSS. All three public brokers expose both transports
on the same broker instance, so a Host on Windows WSS `8884` and a Peer on
Android TLS `8883` pub/sub the same `bluesteal/v1/<code>/*` topics — the
broker routes the MQTT packets between them transparently.

```kotlin
// Broker table — Android only
val HiveMQ    = Broker("broker.hivemq.com",   8883, "/mqtt", useTLS = true)
val EMQX      = Broker("broker.emqx.io",      8883, "/mqtt", useTLS = true)
val Mosquitto = Broker("test.mosquitto.org",  8883, "/mqtt", useTLS = true)

// client builder — note the absence of `.webSocketConfig()`
MqttClient.builder()
    .useMqttVersion3()
    .identifier(...)
    .serverAddress(resolveIPv4(broker.host, broker.port))
    .sslWithDefaultConfig()
    .buildAsync()
```

### 11.3 Per-broker budget raised to 10 s on Android

See **Section 2** — Android's first-attempt TLS + MQTT CONNECT carries more
setup overhead (Conscrypt cold-start, Netty bootstrap) than CocoaMQTT /
M2Mqtt do. A 6 s budget killed the connection mid-handshake on real mobile
networks even when the broker was healthy. Documented in the
`perBrokerTimeoutSeconds` constant comment so future readers can find it.

### 11.4 Cross-platform checks specific to Android

Run an Android Host paired with a macOS / Windows / iOS Peer (and vice
versa), and in addition to the eight checks in **Section 9** confirm:

1. Android trace logs `mqtt.resolve.ok { family: "IPv4" }` before each
   `mqtt.connect.attempt`. If `family` is `IPv6`, the resolver hook has
   regressed and the connect will fail instantly with
   `Connection refused: …/[<v6>]:port`.
2. Android trace logs `mqtt.connect.elapsed { outcome: "ok", elapsedMs: ... }`
   in the **hundreds of ms** range — not at the 10 s budget ceiling. If
   `elapsedMs ≈ 10000` consistently across all brokers, the TLS handshake is
   failing silently and 11.2 / 11.3 need re-investigation.
3. Both sides land on the same broker hostname (`broker.hivemq.com`, etc.)
   even though Android connects on port `8883` while the other side connects
   on `8884` / `8084` / `8081`. The `bluesteal/v1/<code>/*` topics route
   across transports.
4. `dc.send {type:heartbeat}` and matching `dc.message {type:heartbeat}`
   tick every ~4 s on both sides exactly as for same-platform pairing.

### Files

| Concern | Location |
|---------|----------|
| IPv4 pre-resolve | `services/SignalingClient.kt :: resolveIPv4` |
| TLS-MQTT broker table | `services/SignalingClient.kt :: Broker.Companion` |
| Bumped per-broker budget | `services/SignalingClient.kt :: perBrokerTimeoutSeconds` |
| Connect timing log | `mqtt.connect.elapsed { elapsedMs, outcome }` |
