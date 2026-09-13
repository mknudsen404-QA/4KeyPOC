# Composite CDC + MSC Spike

Standalone proof-of-concept for the
[plug-and-play installer plan](../../docs/design/plug-and-play-installer-plan.md).
Deliberately separate from `firmware/neokey/codex_micro_neokey.ino` — no
NeoKey/seesaw code, no shared state — so it's safe to flash to a spare
ESP32-S3 board without touching the working NeoKey firmware.

Demonstrates an ESP32-S3 enumerating as a composite USB device: CDC
(serial) exactly as the NeoKey firmware already does, plus MSC (a tiny
read-only virtual drive) at the same time, backed by a FAT12 image built
in RAM rather than a dedicated flash partition.

Status: compile-verified (`arduino-cli compile`, no upload) against the
installed `esp32:esp32:esp32s3` core 3.3.11. Not yet flashed to physical
hardware — see the plan doc's "Spike findings" section for what's left.

## Board settings

- Board: `esp32:esp32:esp32s3`
- USB Mode: USB-OTG (TinyUSB) — `USBMode=default`
- USB CDC On Boot: Enabled — `CDCOnBoot=cdc`
- Flash size / partition scheme: whatever the board already uses is fine —
  this sketch doesn't need a dedicated flash partition.

```
arduino-cli compile --fqbn esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc firmware/msc_cdc_spike
arduino-cli upload -p /dev/cu.usbmodemXXXX --fqbn esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc firmware/msc_cdc_spike
```

## What to check once flashed

- Board still enumerates and a serial monitor shows the periodic "CDC
  alive, MSC volume mounted alongside it" ping.
- A "SWITCHBD" volume appears in Finder with a `README.TXT` file readable
  in it.
- Known limitation: the FAT12 image only supports 8.3 short filenames, so
  this spike ships `README.TXT`, not a `.command` file — see the plan
  doc for why and what the real installer volume needs instead (VFAT
  long-filename directory entries).
