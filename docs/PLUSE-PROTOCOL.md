# KAT Walk C2+ "plusE" USB protocol - decoded from Windows captures

Source: two USBPcap captures of the official KAT Gateway driving the hardware
(2026-06-23), dissected with `dev/parse_usbpcap.py`.
- Capture 1 (`captures/usbpcap3.pcap`): base only - rotate + vibration test.
- Capture 2 (`captures/run-20260623-134928/usbpcap3.pcap`): base + seat + both
  shoes + armband; seat raise/lower, foot slides, armband heart rate.

## Device topology (VID `0xc4f4`, three USB devices behind a hub)
Roles confirmed from USB **string descriptors** ("walk c2 plusE …"):

| addr | PID  | string descriptor | serial      | role |
|------|------|-------------------|-------------|------|
| 24   | bf12 | …position         | (per unit)  | base/desktop position + foot ground-contact |
| 25   | 3f12 | …receiver         | (per unit)  | **main stream**: body IMU + both feet |
| 26   | bf13 | …armband          | (per unit)  | armband: heart rate + battery |

Endpoints per device: `EP0x01` interrupt **OUT** = host→device commands;
`EP0x81` interrupt **IN** = sensor stream; `EP0x80` control **IN** = pure USB
descriptor polling (device/config/string), **no sensor data** - ignore it.

## Frame format (CONFIRMED)
Fixed **32-byte** frames, zero-padded, both directions:

```
off 0  1  2  | 3  4 | 5    | 6       | 7 ...
    1f 55 aa | xx 00| type | subtype | payload
```
- `1f 55 aa` header (the documented C2 framing - it is correct).
- off 3: usually `00`; `05` on the position device's foot-contact frame,
  `04`/`05` on some - acts as a sub-stream tag, not a length.
- `type` (off 5) + `subtype` (off 6) select the payload meaning (table below).
- Idle/no-data payloads use the sentinel `… 4e 20 00 64 05 …`
  (0x4e20=20000, 0x64=100, 0x05=5 - config constants, NOT live data).

## Receiver stream - dev25 / EP0x81 (the one the driver should read)
| type | sub | meaning | rate | payload |
|------|-----|---------|------|---------|
| 0x30 | 0x00 | **body orientation** | ~48 Hz | off 7: 4×int16 LE, ×2⁻¹⁴ = unit quaternion (CONFIRMED \|q\|=1.000) |
| 0x30 | 0x01 | **left foot** | ~48 Hz | X=int16 LE @off21, Y=int16 LE @off23 (CONFIRMED) |
| 0x30 | 0x02 | **right foot** | ~48 Hz | same layout as left |
| 0x32 | 0/1/2 | **per-sensor status** | ~0.2 Hz | **off10 = battery %, off11 = firmware major**; sub00=Direction, sub01=Left foot, sub02=Right foot (CONFIRMED exact vs app) |
| 0x33 | 0x00 | event | very low | `00 00 09 00 64 …` |
| 0x05 | 0x00 | **receiver self-report** (init) | once | **off7 = receiver firmware major** (V3) |
| 0x21 | 0x00 | desktop calibration / position | 3 | two float32 triples (off 7+) |
| 0x05/0x31 | – | init/handshake replies | once | - |

Left/right confirmed by timing: sub01 movement began when the left shoe was
taken off the charger (~125 s), sub02 ~23 s later with the right shoe.

**Foot position (CONFIRMED via single-axis capture
`run-20260623-141656`):** two signed int16 LE, centred at 0:
- **X @ off 21** = lateral / strafe (observed ±~500).
- **Y @ off 23** = fore/aft (observed −516 … +652).

Axis isolation was clean (forward slides moved only Y, side slides only X).
**Inversion (important):** the sensor reads the shoe sliding on the deck, so
shoe-slides-backward → **+Y → user walks FORWARD**; shoe-forward → −Y →
backward. Each touchdown re-centres to 0, then ramps to the slide extreme.
off 9–10 is a separate status/quality field (not position).
Scale (counts → m/s) still needs calibration against a known stride.

## Seat / Vehicle Hub - dev24 / EP0x81  (bf12 "position")
dev24 is the **Vehicle Hub (seat)**, confirmed by a seat-only capture
(base off, only dev24 streamed).

| type | off3 | meaning | payload |
|------|------|---------|---------|
| 0x40 | 0x05 | seat status | off7=`02` status / `00` connect-event; **off8 = battery RAW**; off9 = connected/active bit |
| 0x02 | 0x04 | version/magic (once) | `… 37 01 …` - constant, shared with armband (NOT battery) |

**Seat battery** is at **off8 of the `type40` status frame (off7==0x02)** as a
RAW value, not a percent: `0x9c`(156)→54%, `0xb4`(180)→60% (confirmed by a
battery swap). Local linear fit **% ≈ off8/4 + 15**; the app applies its own
raw→% curve (reads conservative - a 99%-on-charger cell showed 60%). The
connect-event frame is `… 40 00 00 00 01 01` (off7/8 = 0).

dev24/EP0x01 also receives the all-zero keepalive. Foot "on ground" (the app's
white dot) is derivable from the receiver foot stream: idle sentinel = lifted,
live X/Y = on deck.

## Battery + firmware - CONFIRMED exact against the app
The `type32` status frame (≈every 5 s) carries BOTH per-sensor battery and
firmware. The receiver's own firmware is in its `type05` init frame.

| sensor | frame | battery | firmware | verified |
|--------|-------|---------|----------|----------|
| Receiver | dev25 `type05` | (USB powered) | off 7 = **V3** | matches app |
| Direction (base) | dev25 `type32 sub00` | off 10 | off 11 = **V4** | matches app |
| Left foot  | dev25 `type32 sub01` | off 10 | off 11 = **V5** | matches app |
| Right foot | dev25 `type32 sub02` | off 10 | off 11 = **V5** | matches app |
| Armband    | dev26 `type40` | off 7 | – | (see below) |

Battery drained monotonically 82/100/100 → 75/90/85 across the session,
tracking the app's readout exactly. Firmware was constant (V3/V4/V5/V5) - the
app's "populates on the fly" is just these streaming status frames; the `Null`
state is pre-first-frame. **Trap:** the `0x64`=100 in the idle-foot sentinel
(`…00 64 05…`) is a config constant, NOT battery - don't confuse them.

## Armband - dev26 / EP0x81 (CONFIRMED)
| type | meaning | payload |
|------|---------|---------|
| 0x40 | heart rate + battery | off 7 = battery %, **off 9 = heart-rate bpm** |

Verified: off 9 swept 69–79 bpm, matching the reported 74–75; battery latched
to `0x64` once read.

## Commands (host → device, EP0x01)
- **Keepalive / "stay on":** all-zero 32-byte frame to **dev24/EP0x01**,
  continuous (~3.5/s) for the whole session. Stops on Gateway close → base
  drops to slow-blink. This was the piece missing on Linux.
- **Init (dev25):** bare type frames `…31`, `…05`, `…21`, then `…30` repeating.
- **Vibration (dev25):** `1f 55 aa 00 00 a1 00 02 <intensity_be16>`;
  capture1 level5=`0341`(833)/level1=`00a6`(166) → intensity≈level×166;
  capture2 seat-feedback used `01f4`(500). OFF=`0000`.
  `a1 01 02 00 01` = enable-vibration toggle.

## Clean shutdown / sleep (CONFIRMED from two Gateway-close captures)
The Gateway puts the sensors to sleep on exit with this exact sequence
(run-20260624-195718 and run-145059 are identical):
1. **Stop the keepalive** (and any `0x30` poll) - keepalive's last frame goes out
   *before* the sleep commands.
2. `1f 55 aa 00 00 a0 00 02 00 00` → dev25/EP0x01  (the sleep command; `a0`
   appears ONLY at shutdown, never during operation).
3. `1f 55 aa 00 00 31` → dev25/EP0x01, once  (stop-stream; the receiver's
   ~250 Hz IN stream dies ~50 ms later).
4. **Hold the USB connection open and idle for ~4–5 s** - the Gateway sends
   nothing for 4.6 s before tearing down the interface. This idle-but-connected
   window appears to be what lets sleep propagate to the wireless base/shoes;
   closing the handle immediately leaves them to a slow RF idle-timeout instead
   (the "sensors stay on" bug). Then SET_IDLE the seat/armband HID ifaces & close.

**Linux implementation note (CONFIRMED on hardware):** SET_IDLE + the idle hold are
NOT sufficient on Linux - the `usbhid` kernel driver polls the IN endpoint forever,
which keeps the devices awake (this is *the* difference from Windows). The fix
(`katwalk/daemon.py`): after a0/31 + the ~4.5 s hold, **detach `usbhid`**
via usbfs `USBDEVFS_DISCONNECT_CLAIM` on bf12 (base) + bf13 (armband) - this both stops
the polling *and* lets the otherwise-`EBUSY` SET_IDLE control transfer through - send
SET_IDLE, then close (the kernel auto-reattaches `usbhid`; the device stays asleep until
the next `0x30` poll + motion). Needs usbfs access - the `SUBSYSTEM=="usb"` rule in
`udev/70-katvr.rules`. The receiver's hidraw must be closed before the detach (else EBUSY).

## Still open
- Foot X/Y → m/s scale factor (encoding confirmed; needs a known-stride calib).
- **Seat / Vehicle-Hub battery + firmware (~54–58%)** - not in any capture; the
  hub never streamed a `type32` frame (no sub03), and `usbpcap1/2` were empty.
  Needs a capture with the hub actively connected/paired.
- `0x21` calibration float triples interpretation.
- LED brightness command (not yet exercised).
