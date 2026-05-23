# macOS Parity Updates

Changes needed in `bluesteal-macos` to reach feature parity with the Windows port.

---

## 1. Add `AdvertVariant.googleMfgV1`

**File:** `bluesteal/Models/AdvertVariant.swift`

Add a new case to the `AdvertVariant` enum:

```swift
/// Google manufacturer-data caBLE v1: Manufacturer ID 0x00E0, header [length, 0x15, 0x20, 0x01], 16-byte EID.
case googleMfgV1
```

Update `badge` and `wireName` computed properties to return `"Google-Mfg"` / `"googleMfgV1"`.

---

## 2. Extend `CableScanner.swift` to inspect manufacturer data

**File:** `bluesteal/Services/CableScanner.swift`

In `centralManager(_:didDiscover:advertisementData:rssi:)`, the current code only checks `CBAdvertisementDataServiceDataKey`. Also inspect `CBAdvertisementDataManufacturerDataKey`:

```swift
let manufacturerData = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data
```

The raw manufacturer data from CoreBluetooth on macOS is prefixed with a 2-byte little-endian company ID. Parse it:

- Extract `companyId = UInt16(littleEndian:)` from bytes [0..2].
- If `companyId == 0x00E0` and remaining length == 18 (4-byte header + 16-byte EID = 20 total after company ID removal... *actually* CoreBluetooth includes the company ID, so total Data.count == 22, company bytes + header + EID):
  - Verify `data[2] == 20` (length), `data[3] == 0x15` (type), `data[4] == 0x20` (flags), `data[5] == 0x01` (version).
  - EID = `data[6..<22]`.

Forward the event even if no service data is present, as long as manufacturer data matches.

---

## 3. Update `AdvertClassifier.swift`

**File:** `bluesteal/Services/AdvertClassifier.swift`

Add a new classification method:

```swift
static func classifyManufacturerData(_ data: Data) -> (variant: AdvertVariant, eid: Data)? {
	guard data.count == 22 else { return nil }
	let companyId = data.withUnsafeBytes { $0.load(as: UInt16.self) }
	guard companyId == 0x00E0 else { return nil }
	guard data[2] == 20, data[3] == 0x15, data[4] == 0x20, data[5] == 1 else { return nil }
	return (.googleMfgV1, data[6..<22])
}
```

---

## 4. Update `AdvertStore.swift`

**File:** `bluesteal/Services/AdvertStore.swift`

In `ingest(_:)`, after iterating `event.serviceData`, also check:

```swift
if let mfgData = event.manufacturerData,
   let (variant, eid) = AdvertClassifier.classifyManufacturerData(mfgData) {
	// Upsert with serviceUUID = nil, treat eid as the payload
}
```

Update `CableAdvert` to allow `serviceUUID` to be optional (or add `manufacturerId: UInt16?`).

---

## 5. (Future) UUID-as-EID detection (macOS publisher path)

In Chromium's `IS_MAC` branch of `ConstructAdvertisementData`, the client EID is advertised as a **128-bit service UUID** (not service data). The current macOS scanner does not extract EIDs from `CBAdvertisementDataServiceUUIDsKey`. This is a lower-priority enhancement since it only captures adverts from *other* macOS Chrome instances acting as authenticators.

Reference: `fido_cable_discovery.cc` → `GetUUIDs()` method.

---

## References

- Chromium: `device/fido/cable/fido_cable_discovery.cc` (`ConstructAdvertisementData`, `IS_WIN` branch)
- Chromium: `device/fido/cable/fido_ble_uuids.h` (UUID constants)
- Chromium: `device/fido/cable/v2_constants.h` (`kAdvertSize = 16 + 4`)

---

## 6. Peer-to-peer advert sharing — schema relaxations

The Windows port (`bluesteal-windows`) now implements the same WebRTC data-channel relay as macOS commit `ce8d2307`. During integration the following cross-platform wire issues were identified; the macOS side should adopt these fixes for full interop:

### 6a. Make `RelayMessage.Advert.serviceUUID` optional

**File:** `bluesteal/Models/RelayMessage.swift` — change `var serviceUUID: String` → `var serviceUUID: String?`

**File:** `bluesteal/Services/AdvertRelay.swift` — in `init?(wire:)`, guard:
```swift
let serviceUUID: CBUUID? = wire.serviceUUID.flatMap { $0 == "0000" ? nil : CBUUID(string: $0) }
```

**Why:** Windows manufacturer-only adverts (e.g. `googleMfgV1`) have no service UUID. The Windows relay currently sends `"0000"` as a placeholder because this field is non-optional. Once optional, Windows will send `null` instead.

### 6b. Add `manufacturerId: UInt16?` to `RelayMessage.Advert`

Add the field alongside `manufacturerHex`. The Windows side classifies via company ID; without this field the macOS side must re-parse `manufacturerHex` and may misclassify.

### 6c. Extend `AdvertVariant(wireName:)` to recognise `"googleMfgV1"`

**File:** `bluesteal/Services/AdvertRelay.swift`

```swift
case "googleMfgV1": self = .googleMfgV1
```

Currently falls through to `.unknown(reason: "googleMfgV1")`, which means relayed manufacturer-data adverts from Windows render with no badge.

### 6d. `peripheralID` UUID convention (documentation only)

Windows encodes its 48-bit Bluetooth address as the trailing 12 hex characters of an otherwise-zero UUID: `00000000-0000-0000-0000-XXXXXXXXXXXX`. macOS should treat this field as opaque (already the case) and not depend on it for identity beyond display.

### 6e. (Future) Loop guard before BLE re-broadcast

Once either side re-emits received adverts on the air, both sides must add an "origin" / "seenIds" gate to prevent infinite echo. No code change yet — flagged here for the second half of the feature.

### 6f. (Future) Heartbeat `knownIds` reconciliation

The `Heartbeat.knownIds` array is sent every 5 seconds but never consumed on either side. If not used by the re-broadcast feature, consider dropping the field to avoid bloat on busy scans.

---

### Windows relay source files (for cross-reference)

| File | Role |
|------|------|
| `Bluesteal/Models/RelayMessage.cs` | Wire types (`RelayMessage`, `RelayAdvert`, `RelayHeartbeat`) + JSON converter |
| `Bluesteal/Models/SignalingMessage.cs` | MQTT handshake wire types |
| `Bluesteal/Services/SignalingClient.cs` | MQTT-over-WSS pairing client |
| `Bluesteal/Services/RelayClient.cs` | WebRTC `RTCPeerConnection` wrapper |
| `Bluesteal/Services/AdvertRelay.cs` | AdvertStore ↔ RelayClient bridge |

---

## 7. Duplicate-payload warning toast

**Files (macOS):** new `bluesteal/Services/DuplicatePayloadDetector.swift`, plus a banner/toast view in the main window.

The Windows port now warns the user when the host and the peer have both observed an advert with the **same payload bytes** — a strong indicator that both devices are within Bluetooth range of the same caBLE advertiser, which defeats the purpose of the CTAP hybrid relay. macOS should mirror this.

### Behavior

- Compare on **payload hex only** (lowercase). Do **not** include the peripheral UUID / Bluetooth address or the variant — those legitimately differ between host and peer for the same advert.
- Maintain two `Set<String>` collections: locally-observed payload hex and peer-received payload hex.
- Hook the macOS equivalents of `AdvertStore.OnIngest` (every local upsert) and `AdvertRelay`'s received path (every inbound upsert from the peer).
- On every event, insert into the matching set and check for membership in the opposite set. On the **first** intersection, raise a `onDuplicateDetected` callback and latch a `warned` flag so subsequent matches are ignored for the rest of the session.

### UI surface

- Show a warning banner/toast in the main window using whatever transient notification pattern the macOS app already uses (SwiftUI `.alert`, a custom banner view, or `NSAlert`). Severity = warning, user-dismissible, with an automatic dismissal after ~10 seconds to match the Windows `InfoBar`.

### Wording (must match Windows verbatim)

- **Title:** `Host and peer are seeing the same advert`
- **Message:** `Bluesteal works best when host and peer are not within range of the same Bluetooth device for the CTAP hybrid flow.`

### Windows reference

| File | Role |
|------|------|
| `Bluesteal/Services/DuplicatePayloadDetector.cs` | Payload-hex set intersection, once-per-session firing |
| `Bluesteal/Services/AdvertRelay.cs` (`OnReceived`) | New event raised on every peer upsert |
| `Bluesteal/MainWindow.xaml` (`DuplicateWarningBar`) | `<InfoBar Severity="Warning">` host |
| `Bluesteal/MainWindow.xaml.cs` (`OnDuplicateDetected`) | Opens the bar and starts the 10 s auto-dismiss `DispatcherQueueTimer` |

---

## Shorten Pairing Code to 5 Uppercase Letters

**Problem:** The 8-character alphanumeric pairing code was difficult to type across devices manually.

**Fix (Windows):** Reduced to 5 characters using a letters-only alphabet. The code is only used as an MQTT topic for the short-lived pairing window — 5 letters from 23 characters gives ~6.4 M combinations, more than sufficient.

### Changes needed in macOS

**File:** `bluesteal-macos/Sources/Services/SignalingClient.swift`

1. Change the code alphabet to uppercase letters only (excluding ambiguous I, L, O):

```swift
private static let codeAlphabet = Array("ABCDEFGHJKMNPQRSTUVWXYZ")
```

2. Change `makeCode()` to generate 5 characters:

```swift
static func makeCode() -> String {
    let bytes = (0..<5).map { _ in UInt8.random(in: 0...255) }
    return String(bytes.map { codeAlphabet[Int($0) % codeAlphabet.count] })
}
```

3. Update any join/input validation to accept exactly 5 characters instead of 8.

4. Always display and accept codes in uppercase (`uppercased()` on input).

### Important

The alphabet and length **must match Windows exactly** — both sides hash the code to select the starting MQTT broker. A mismatch means host and peer pick different brokers and pairing fails.

---

## 8. Re-broadcast peer adverts over local BLE peripheral

When the relay is open, adverts received from the peer should be re-emitted on the local Bluetooth peripheral so a nearby CTAP hybrid client (e.g. Chrome on a laptop) can observe them.

### Design (mirrors Windows implementation)

| Aspect | Behaviour |
|--------|-----------|
| Source | Peer-received adverts only (via `AdvertRelay`'s received pipeline) |
| Selection | Latest-wins — single `CBPeripheralManager` advertisement at a time; newest non-stale advert is broadcast |
| TTL | 8 seconds since `lastSeen`; a 2-second sweep timer expires stale entries |
| Debounce | 250 ms minimum between publisher swaps |
| Loop guard | Maintain a `Set<String>` of self-published payload-hex; `AdvertStore.ingest` drops events matching this set |
| Lifecycle | Starts automatically when relay opens; stops on relay close/disconnect |

### New files

- **`bluesteal/Services/BleAdvertiser.swift`** — wraps `CBPeripheralManager`.
  - **CoreBluetooth limitation:** `peripheralManager.startAdvertising(_:)` only accepts `CBAdvertisementDataLocalNameKey` and `CBAdvertisementDataServiceUUIDsKey` for third-party apps. Raw service-data and manufacturer-data cannot be emitted.
  - For `FidoV2` / `GoogleV2` adverts: emit the EID encoded as a **128-bit service UUID** (matches Chromium's `IS_MAC` branch in `ConstructAdvertisementData` which encodes the client EID into UUID octets). UUID form: `0000FFF9-<EID bytes mapped into the UUID>`.
  - For `GoogleMfgV1` adverts (manufacturer-data only): **cannot be re-broadcast on macOS**. Log a `"not re-broadcastable on macOS (manufacturer-data)"` warning and skip.
  - Exposes: `startAdvertising(uuid:)`, `stopAdvertising()`, `isAdvertising`, `onStatusChanged`.

- **`bluesteal/Services/RebroadcastService.swift`** — mirrors Windows `RebroadcastService.cs`:
  - Subscribes to `AdvertRelay`'s received events.
  - Runs a 2-second `Timer` for stale-sweep.
  - Manages `selfPublishedPayloadHex: Set<String>`.
  - Publishes `@Published var activeCount: Int`, `@Published var isActive: Bool`, `@Published var lastError: String?`.
  - Calls `BleAdvertiser.startAdvertising` / `stopAdvertising`.

### Modified files

- **`bluesteal/Services/AdvertStore.swift`** — in `ingest(_:)`, check `RebroadcastService.shared.isSelfPublished(payloadHex)` and drop matches (loop guard).
- **Main window / RelayView (SwiftUI)** — per-row green dot on peer advert rows when `isRebroadcasting == true`; "Rebroadcasting N" chip on the relay status bar (green when active, red + error text on failure).

### Capabilities / entitlements

- Confirm `NSBluetoothAlwaysUsageDescription` is set in `Info.plist` — required by `CBPeripheralManager`.
- No additional entitlements needed for BLE peripheral role.

### Cross-reference

This fulfils the loop-guard requirement described in **Section 6e** ("origin / seenIds gate to prevent infinite echo"). Both Windows and macOS now maintain a self-published payload set that prevents re-ingesting their own re-broadcasts.

### Windows reference files

| File | Role |
|------|------|
| `Bluesteal/Services/BleAdvertiser.cs` | `BluetoothLEAdvertisementPublisher` wrapper |
| `Bluesteal/Services/RebroadcastService.cs` | Latest-wins coordinator, TTL sweep, self-payload guard |
| `Bluesteal/Services/AdvertStore.cs` | `selfPayloadFilter` loop guard in `Ingest` |
| `Bluesteal/ViewModels/CableAdvertViewModel.cs` | `IsRebroadcasting` property for UI binding |
| `Bluesteal/Views/RelayView.xaml(.cs)` | "Rebroadcasting N" chip |

