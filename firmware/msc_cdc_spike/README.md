# Composite CDC + MSC Spike

Standalone proof-of-concept for the
[plug-and-play installer plan](../../docs/design/plug-and-play-installer-plan.md).
Deliberately separate from `firmware/neokey/codex_micro_neokey.ino` — no
NeoKey/seesaw code, no shared state — so it's safe to flash to a spare
ESP32-S3 board without touching the working NeoKey firmware.

Demonstrates an ESP32-S3 enumerating as a composite USB device: CDC
(serial) exactly as the NeoKey firmware already does, plus MSC (a small
read-only virtual drive) at the same time, backed by a FAT12 image with
proper VFAT long-filename entries, built in RAM/flash `.rodata` rather
than a dedicated flash partition. The volume carries the real
`Install Switchboard.command` installer stub — see that file for the
actual install logic.

**Status: validated end-to-end on real hardware.** Flashed to the working
NeoKey board (with explicit sign-off, then reflashed back to
`codex_micro_neokey.ino` afterward) — see the plan doc's "Spike findings"
section for the full validation writeup, including a real-world caveat
about the NeoKey chip occasionally needing a full power cycle after heavy
flash/reset cycling (a pre-existing, documented chip quirk, not something
this spike introduced).

## Regenerating the disk image

`installer_disk.h` is generated, not hand-written. After editing
`Install Switchboard.command`, regenerate it with:

```
python3 gen_fat.py "Install Switchboard.command"
```

This also writes `fat_image.bin`, a raw disk image you can sanity-check
locally before ever touching hardware:

```
hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount fat_image.bin
diskutil mount /dev/diskN   # use the disk id hdiutil just printed
ls "/Volumes/SWITCHBD"      # (or "SWITCHBD 1" if another SWITCHBD volume is already mounted)
hdiutil detach /dev/diskN
```

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

Native-USB auto-reset into bootloader mode was unreliable in testing —
if `arduino-cli upload` fails with "No serial data received", manually
enter bootloader mode first (hold BOOT, tap RESET, release BOOT), then
retry against the port that appears (usually a lower port number, since
there's no CDC in bootloader mode).

## What to check once flashed

- Board enumerates and a serial monitor (or `arduino-cli monitor`) shows
  the periodic "CDC alive, MSC volume mounted alongside it" ping.
- A "SWITCHBD" volume appears in Finder containing
  `Install Switchboard.command`.
- Since the bridge is presumably already installed on your dev Mac,
  double-clicking that file is safe to test live — it detects the
  existing `~/Library/LaunchAgents/com.switchboard.bridge.plist` and just
  shows an "already installed" dialog, no clone, no changes.
