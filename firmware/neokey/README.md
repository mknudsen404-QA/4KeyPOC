# NeoKey Switchboard Client

ESP32-S3 (generic dev board) + Adafruit NeoKey 1x4 (seesaw, I2C address
0x30). Speaks the same JSON-lines protocol over USB serial as `host/`'s
`switchboard_bridge.py` — see that project's `README_BRIDGE.md` for the
protocol and how to run the bridge against this board.

Keys A/B/C select agent slots 1/2/3; key D is push-to-talk for whichever
slot is currently selected.

## Board setup

- Board: `esp32:esp32:esp32s3`, with **USB CDC On Boot = Enabled**
  (`CDCOnBoot=cdc`) — this board has no separate USB-UART chip, so this is
  required for the serial monitor to work at all — **and USB Mode =
  USB-OTG (TinyUSB)** (`USBMode=default`), required for the composite
  CDC+MSC installer drive below. A build with the wrong USB Mode fails
  with a `#error` rather than silently shipping a board that can't
  advertise the drive.
  ```
  arduino-cli compile --fqbn esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc firmware/neokey
  ```
- Wiring: NeoKey `C` (SCL) -> GPIO8, NeoKey `D` (SDA) -> GPIO9, NeoKey VIN ->
  3.3V, NeoKey's unlabeled pin (GND) -> GND.
- **The NeoKey's header pins must be soldered**, not just friction-fit into a
  breadboard — an unsoldered/poorly-seated connection here reads as "no I2C
  device found" and is easy to mistake for a wiring or code problem.

## Plug-and-play installer drive

As of 2026-10-04 this firmware also enumerates a small read-only USB
drive ("SWITCHBD") alongside its normal serial port — merged in from the
validated `firmware/msc_cdc_spike/` spike (see
`docs/design/plug-and-play-installer-plan.md`). On a fresh Mac with
nothing installed, the drive appears in Finder containing
`Install Switchboard.command`; double-clicking it clones the (public)
repo and runs `host/setup.sh`, or shows an "already installed" dialog if
it finds the bridge's LaunchAgent already present. This is what makes
handing a board to someone else (a friend, a coworker) close to actual
plug-and-play instead of "open a terminal and paste two commands."

The drive's one file is a compiled-in FAT image (`installer_disk.h`),
generated offline, not read from flash at runtime. After editing
`Install Switchboard.command`, regenerate it with:

```sh
cd firmware/neokey
python3 gen_fat.py "Install Switchboard.command"
```

This also writes `fat_image.bin` next to it, which you can sanity-check
locally before ever touching hardware — see
`firmware/msc_cdc_spike/README.md`'s "Regenerating the disk image"
section for the `hdiutil`/`diskutil` mount steps (identical process,
only the source `.ino`/output path differ).

## What you should see on the serial port at boot

Right after `Serial.begin()` (before the seesaw is touched):

```json
{"event":"boot","stage":"start","firmware":"neokey","build":"<compile date/time>"}
```

If the seesaw doesn't answer, one `error` line follows per retry (~1/s,
retried forever — the board is useless without it):

```json
{"event":"error","reason":"seesaw_no_response","attempt":1}
```

Once init succeeds and the startup LED sweep finishes:

```json
{"event":"boot","stage":"ready","firmware":"neokey","attempts":0}
```

A board stuck repeating `error` lines has a hung seesaw chip (see the quirk
below) — power-cycle it (unplug, wait 5s, replug). A board that never
prints anything at all, ever, on any key press or replug, is very likely
running the wrong firmware — see
`docs/design/wrong-firmware-recovery-plan.md`.

## Libraries (install via Arduino IDE Library Manager, or `arduino-cli lib install`)

- `Adafruit_seesaw_Library` (provides `Adafruit_NeoKey_1x4`)
- `ArduinoJson` (v7)

## Testing the LED logic without a board

The pure LED rendering logic (status colors, the busy-turn ramp, pulse
timing) lives in `led_model.h`, which has no Arduino includes and can be
built and run with a plain host C++ compiler:

```sh
firmware/neokey/test/run.sh
```

To catch build breaks without a board attached, `firmware/neokey/test/compile.sh`
does a compile-only `arduino-cli compile` of the whole sketch (no upload).

## A real hardware quirk this code works around

`Adafruit_NeoKey_1x4::begin()` (and the `seesaw_NeoPixel::begin()` it calls
internally) issues a seesaw software-reset before anything else. On this
specific board, that reset reliably leaves the chip unable to answer any
further I2C register reads — confirmed with a raw I2C diagnostic sketch: a
plain address probe and even a full register read work fine, but the moment
a software reset is sent, subsequent reads come back as `0xFF` garbage,
consistently, even across a full power cycle. Root cause was never fully
pinned down (tried longer delays, slower I2C clock, re-initializing the bus
— none of it helped), so this code just avoids the reset entirely: it
manually replicates what `begin()` does, calling
`Adafruit_seesaw::begin(addr, -1, /*reset=*/false)` directly on both the
`neokey` object and its `.pixels` member, then configuring the NeoPixel
chain and button pins by hand.
