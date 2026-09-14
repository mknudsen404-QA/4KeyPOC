// Composite CDC + MSC spike for the "plug-and-play installer" project.
// See docs/design/plug-and-play-installer-plan.md for the full plan.
//
// Purpose: prove that Arduino-ESP32's native USB stack can present a
// single ESP32-S3 as CDC (serial, same role as neokey.ino's
// runtime protocol) *and* MSC (a small read-only virtual drive) at the
// same time, on the exact core version this project already has
// installed. Deliberately standalone — no NeoKey/seesaw code, no shared
// state with neokey.ino, so it can be flashed to any spare
// ESP32-S3 board without touching the working NeoKey firmware.
//
// Board settings this sketch requires (Tools menu in Arduino IDE, or the
// equivalent --fqbn options in arduino-cli):
//   Board: esp32:esp32:esp32s3
//   USB Mode: USB-OTG (TinyUSB)      (USBMode=default)
//   USB CDC On Boot: Enabled         (CDCOnBoot=cdc)
//
// The virtual disk (installer_disk.h) is a FAT12 image with VFAT
// long-filename directory entries, generated offline by gen_fat.py from
// the real "Install Switchboard.command" installer stub — see that
// script and firmware/msc_cdc_spike/README.md for how to regenerate it
// after editing the installer script. It lives entirely in RAM/flash
// .rodata as a compiled-in array, not a dedicated flash partition: see
// the plan doc's "Spike findings" section for why that's sufficient.

#include <Arduino.h>
#ifndef ARDUINO_USB_MODE
#error This sketch requires an ESP32-S3 (or other SoC with native USB)
#elif ARDUINO_USB_MODE == 1
#error Set "USB Mode" to "USB-OTG (TinyUSB)", not "Hardware CDC and JTAG"
#else

#include "USB.h"
#include "USBMSC.h"
#include "installer_disk.h"

USBMSC MSC;

static int32_t onRead(uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize) {
  memcpy(buffer, msc_disk_image + (lba * DISK_SECTOR_SIZE) + offset, bufsize);
  return bufsize;
}

static int32_t onWrite(uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize) {
  // Read-only volume: silently accept and discard writes (some OSes probe
  // writability during mount). Never persists anything.
  return bufsize;
}

static bool onStartStop(uint8_t power_condition, bool start, bool load_eject) {
  return true;
}

unsigned long lastPing = 0;

void setup() {
  Serial.begin(115200);

  MSC.vendorID("Switchbd");
  MSC.productID("Installer");
  MSC.productRevision("spike");
  MSC.onRead(onRead);
  MSC.onWrite(onWrite);
  MSC.onStartStop(onStartStop);
  MSC.mediaPresent(true);
  MSC.isWritable(false);
  MSC.begin(DISK_SECTOR_COUNT, DISK_SECTOR_SIZE);

  USB.begin();
}

void loop() {
  // Proves CDC keeps working normally with MSC active alongside it — this
  // is the whole point of the spike. JSON-shaped and self-identifying so
  // the bridge/doctor can tell this apart from firmware/neokey instead of
  // ignoring it as log text (see host/switchboard/doctor.py
  // check_firmware_identity and docs/design/wrong-firmware-recovery-plan.md).
  if (millis() - lastPing > 2000) {
    lastPing = millis();
    if (Serial) {
      Serial.println("{\"event\":\"boot\",\"stage\":\"ping\",\"firmware\":\"msc_cdc_spike\",\"build\":\"" __DATE__ " " __TIME__ "\"}");
    }
  }
}

#endif
