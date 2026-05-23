# Relay Pairing Debugging Guide

This guide explains how to diagnose why two `Bluesteal.exe` instances fail to
pair over the WebRTC relay using the new tracing emitted by `RelayTracer`.

## 1. Where the logs live

Each process writes its own JSONL trace file (one per PID per UTC date):

```
%LOCALAPPDATA%\bluesteal\Logs\relay-YYYY-MM-DD-<pid>.jsonl
```

Every line is a JSON object with the shape:

```json
{
  "ts": "2025-01-01T12:34:56.7890123Z",
  "pid": 12345,
  "instanceId": "a1b2c3d4",
  "role": "Host",
  "code": "ABCD2345",
  "category": "signaling",
  "event": "signaling.publish",
  "data": { "...": "..." }
}
```

`instanceId` is a short GUID assigned at process startup, so you can always
tell the two windows apart even if you forget which PID is which. The same
data is also mirrored to `Debug.WriteLine`, so the Visual Studio Output
window shows the trace live during F5.

There is an **"Open logs"** hyperlink at the right-hand end of the relay bar
in the app that opens this directory in Explorer.

## 2. Running two instances

1. Build once: `dotnet build Bluesteal\Bluesteal.sln`.
2. Open two PowerShell windows.
3. In each, run the produced exe, e.g.
   `C:\Users\James.Westgate\source\repos\bluesteal-windows\Bluesteal\Bluesteal\bin\x64\Debug\net10.0-windows10.0.19041.0\Bluesteal.exe`.
   (Alternatively run from Visual Studio twice — both will get separate PIDs
   and therefore separate log files.)
4. In instance **A**: click **Host**, copy the 8-char code that appears.
5. In instance **B**: click **Join…**, paste the code, click **Connect**.
6. When the run finishes (success or hang), click **Open logs** in either
   instance and tail the two `relay-*-<pid>.jsonl` files.

## 3. The expected signpost sequence

Read the two files in parallel (sort by `ts` to interleave). A successful
pairing produces, in order:

| # | Host log line                                            | Peer log line                                            |
|---|----------------------------------------------------------|----------------------------------------------------------|
| 1 | `app.start`                                              | `app.start`                                              |
| 2 | `relay.host` (code generated)                            | `relay.join` (same code)                                 |
| 3 | `mqtt.connect.attempt` … `mqtt.connected`                | `mqtt.connect.attempt` … `mqtt.connected`                |
| 4 | `mqtt.subscribed` (`/answer`, `/ice/peer`, `/bye`)       | `mqtt.subscribed` (`/offer`, `/ice/host`, `/bye`)        |
| 5 | `pc.createOffer` → `pc.setLocalDescription`              |                                                          |
| 6 | `signaling.publish` (type=`offer`, retain=true)          | `signaling.receive` (type=`offer`, retained=true)        |
| 7 |                                                          | `pc.setRemoteDescription` (type=`offer`, result=`OK`)    |
| 8 |                                                          | `pc.createAnswer` → `pc.setLocalDescription`             |
| 9 |                                                          | `signaling.publish` (type=`answer`)                      |
|10 | `signaling.receive` (type=`answer`)                      |                                                          |
|11 | `pc.setRemoteDescription` (type=`answer`, result=`OK`)   |                                                          |
|12 | `ice.local` / `ice.remote.apply` (host & srflx & relay)  | `ice.local` / `ice.remote.apply`                         |
|13 | `ice.connectionState` → `checking` → `connected`         | same                                                     |
|14 | `dc.open`                                                | `dc.open`                                                |
|15 | `state.change` to=`Open`                                 | `state.change` to=`Open`                                 |

## 4. Failure-mode matrix

Find the **first** signpost that is missing or wrong. The likely cause:

| First missing / wrong line                                            | Likely cause                                                                                                  |
|-----------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| `mqtt.connected` never appears                                        | TLS / WebSocket connect failure on every broker — check the `mqtt.connect.failed` lines for exceptions.       |
| Two instances connect to **different** `host` values in `mqtt.connected` | First broker is up for one machine but down for the other (broker fallback divergence). Need a single broker. |
| Peer never sees `signaling.receive` of type `offer`                   | Host published before peer subscribed, **and** offer was not retained — or peer is on a different broker.     |
| Peer sees a stale `offer` with `retained=true` before `relay.join`    | Old session retained an offer on the broker; need to clear it on disconnect.                                  |
| `pc.setRemoteDescription` shows `result != OK`                        | SDP rejected by SIPSorcery — capture the `data` and diff against macOS offer wire format.                     |
| `signaling.unexpected` appears                                        | Both instances picked the **same role** (two Hosts or two Peers), or a self-echo leaked past `IsOwnOrigin`.    |
| `ice.local` events but no `ice.remote.apply` on the peer side         | ICE candidates being published on a topic the other side isn't subscribed to (check `signaling.publish.topic`).|
| `ice.connectionState` stops at `checking` and never reaches `connected` | Both peers behind symmetric NAT; TURN relay never selected. Check `ice.local` entries for any `typ relay`.    |
| `dc.parse.error` on either side after `dc.open`                       | Wire-format mismatch between Windows/macOS `RelayMessage`. Inspect `jsonPreview`.                             |
| `mqtt.disconnected` fires unexpectedly mid-handshake                  | Broker dropped the connection; inspect `reason` and `exception` fields.                                       |

## 5. Tips

- Use `Get-Content -Wait` in PowerShell to live-tail a log file:
  ```powershell
  Get-Content "$env:LOCALAPPDATA\bluesteal\Logs\relay-*-<pid>.jsonl" -Wait
  ```
- To diff two log files side-by-side after a run, pipe both through
  `ConvertFrom-Json` and select the `event` column:
  ```powershell
  Get-Content A.jsonl | % { $_ | ConvertFrom-Json } | Select ts, category, event
  ```
- Long SDPs are truncated in the log (`sdpPreview` plus `sdpLength`) so the
  files stay grep-friendly. If you need the full SDP, temporarily change the
  `Preview` helpers in `SignalingClient.cs` / `RelayClient.cs` to return the
  raw string.
- The tracer never throws — any IO failure is swallowed and reported only
  through `Debug.WriteLine` so production behaviour is unaffected.
