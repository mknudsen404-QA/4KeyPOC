// Composite CDC + MSC spike for the "plug-and-play installer" project.
// See docs/design/plug-and-play-installer-plan.md for the full plan.
//
// Purpose: prove that Arduino-ESP32's native USB stack can present a
// single ESP32-S3 as CDC (serial, same role as codex_micro_neokey.ino's
// runtime protocol) *and* MSC (a small read-only virtual drive) at the
// same time, on the exact core version this project already has
// installed. Deliberately standalone — no NeoKey/seesaw code, no shared
// state with codex_micro_neokey.ino, so it can be flashed to any spare
// ESP32-S3 board without touching the working NeoKey firmware.
//
// Board settings this sketch requires (Tools menu in Arduino IDE, or the
// equivalent --fqbn options in arduino-cli):
//   Board: esp32:esp32:esp32s3
//   USB Mode: USB-OTG (TinyUSB)      (USBMode=default)
//   USB CDC On Boot: Enabled         (CDCOnBoot=cdc)
//
// The virtual disk lives entirely in RAM (a static FAT12 image), not in a
// dedicated flash partition. See the "spike findings" note in the plan
// doc for why: the real installer payload is a few KB of shell script,
// well within SRAM budget, and this sidesteps flash partition-table
// changes (and any risk of colliding with codex_micro_neokey.ino's
// partition layout) entirely.
//
// KNOWN LIMITATION (tracked, not fixed here): this spike's FAT12 image
// only has classic 8.3 short filenames (max 3-char extension), so it
// ships a "README.TXT" placeholder rather than the real installer's
// ".command" file (7-char extension). The real installer volume will
// need VFAT long-filename (LFN) directory entries to carry a ".command"
// extension — that's straightforward (extra 0x0F-attribute directory
// records) but is deliberately out of scope for this composite-USB spike.

#include <Arduino.h>
#ifndef ARDUINO_USB_MODE
#error This sketch requires an ESP32-S3 (or other SoC with native USB)
#elif ARDUINO_USB_MODE == 1
#error Set "USB Mode" to "USB-OTG (TinyUSB)", not "Hardware CDC and JTAG"
#else

#include "USB.h"
#include "USBMSC.h"

USBMSC MSC;

static const uint32_t DISK_SECTOR_COUNT = 16;  // 8KB: smallest size Windows will mount
static const uint16_t DISK_SECTOR_SIZE = 512;

// Zero-initialized by the C++ runtime; we fill in the interesting bytes
// by hand in buildDisk() instead of via a giant designated-initializer
// literal (fragile to get byte-offsets right in, and range-designators
// aren't portable C++).
static uint8_t msc_disk[DISK_SECTOR_COUNT][DISK_SECTOR_SIZE];

static const char PLACEHOLDER_CONTENTS[] =
  "This is a placeholder for the Switchboard plug-and-play installer.\r\n"
  "The real drive will carry a double-clickable .command file instead\r\n"
  "of this README. See docs/design/plug-and-play-installer-plan.md.\r\n";

static void put16(uint8_t *dst, uint16_t v) {
  dst[0] = v & 0xFF;
  dst[1] = (v >> 8) & 0xFF;
}

static void put32(uint8_t *dst, uint32_t v) {
  dst[0] = v & 0xFF;
  dst[1] = (v >> 8) & 0xFF;
  dst[2] = (v >> 16) & 0xFF;
  dst[3] = (v >> 24) & 0xFF;
}

static void buildDisk() {
  memset(msc_disk, 0, sizeof(msc_disk));

  // --- Block 0: boot sector ---
  uint8_t *boot = msc_disk[0];
  boot[0] = 0xEB;
  boot[1] = 0x3C;
  boot[2] = 0x90;
  memcpy(boot + 3, "MSDOS5.0", 8);
  put16(boot + 11, DISK_SECTOR_SIZE);  // bytes per sector
  boot[13] = 1;                        // sectors per cluster
  put16(boot + 14, 1);                 // reserved sectors
  boot[16] = 1;                        // number of FATs
  put16(boot + 17, 16);                // max root dir entries
  put16(boot + 19, DISK_SECTOR_COUNT); // total sectors (16-bit)
  boot[21] = 0xF8;                     // media descriptor
  put16(boot + 22, 1);                 // sectors per FAT
  put16(boot + 24, 1);                 // sectors per track
  put16(boot + 26, 1);                 // number of heads
  put32(boot + 28, 0);                 // hidden sectors
  put32(boot + 32, 0);                 // total sectors (32-bit, unused)
  boot[36] = 0x00;                     // physical drive number
  boot[37] = 0x00;                     // reserved
  boot[38] = 0x29;                     // extended boot signature
  put32(boot + 39, 0x53424B54);        // volume serial ("SBKT")
  memcpy(boot + 43, "SWITCHBD   ", 11);
  memcpy(boot + 54, "FAT12   ", 8);
  boot[510] = 0x55;
  boot[511] = 0xAA;

  // --- Block 1: FAT12 table ---
  // Entries 0/1 reserved (media descriptor + EOF marker), entry 2 is our
  // one file's single cluster, marked end-of-chain.
  uint8_t *fat = msc_disk[1];
  fat[0] = 0xF8;
  fat[1] = 0xFF;
  fat[2] = 0xFF;
  fat[3] = 0xFF;
  fat[4] = 0x0F;

  // --- Block 2: root directory ---
  uint8_t *root = msc_disk[2];
  // Entry 0: volume label
  memcpy(root + 0, "SWITCHBD   ", 11);
  root[11] = 0x08;  // ATTR_VOLUME_ID
  // Entry 1 (32 bytes later): README.TXT
  uint8_t *entry = root + 32;
  memcpy(entry + 0, "README  TXT", 11);
  entry[11] = 0x20;  // ATTR_ARCHIVE
  put16(entry + 26, 2);  // starting cluster
  put32(entry + 28, sizeof(PLACEHOLDER_CONTENTS) - 1);  // file size, no NUL

  // --- Block 3: file contents (cluster 2 == data sector 2, i.e. block index 3
  //     given 1 reserved + 1 FAT + 1 root-dir sector ahead of the data area) ---
  memcpy(msc_disk[3], PLACEHOLDER_CONTENTS, sizeof(PLACEHOLDER_CONTENTS) - 1);
}

static int32_t onRead(uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize) {
  memcpy(buffer, msc_disk[lba] + offset, bufsize);
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

  buildDisk();

  MSC.vendorID("Switchbd");
  MSC.productID("Installer");
  MSC.productRevision("1.0");
  MSC.onRead(onRead);
  MSC.onWrite(onWrite);
  MSC.onStartStop(onStartStop);
  MSC.mediaPresent(true);
  MSC.isWritable(false);
  MSC.begin(DISK_SECTOR_COUNT, DISK_SECTOR_SIZE);

  USB.begin();
}

void loop() {
  // Proves CDC keeps working normally with MSC active alongside it —
  // this is the whole point of the spike.
  if (millis() - lastPing > 2000) {
    lastPing = millis();
    if (Serial) {
      Serial.println("CDC alive, MSC volume mounted alongside it");
    }
  }
}

#endif
