#!/usr/bin/env python3
"""Builds a tiny read-only FAT12 image with one VFAT-long-named file.

Generates:
  - a raw disk image (fat_image.bin) so it can be verified locally with
    hdiutil before ever touching hardware
  - a C header (installer_disk.h) with the same bytes as a static array,
    for the Arduino sketch to serve verbatim over USB MSC.
"""
import sys

SECTOR_SIZE = 512
LONG_NAME = "Install Switchboard.command"
SHORT_NAME_BASE = "INSTALL"   # 8.3 short name: INSTALL~1.CMD (fallback name)
SHORT_NAME = "INSTALL~1"
SHORT_EXT = "CMD"

with open(sys.argv[1], "rb") as f:
    content = f.read()

def lfn_checksum(short_name_83: bytes) -> int:
    assert len(short_name_83) == 11
    csum = 0
    for b in short_name_83:
        csum = (((csum & 1) << 7) + (csum >> 1) + b) & 0xFF
    return csum

def short_name_83_bytes() -> bytes:
    name = (SHORT_NAME.ljust(8)[:8] + SHORT_EXT.ljust(3)[:3]).encode("ascii")
    return name

def lfn_entries(long_name: str, short_83: bytes):
    """Build VFAT LFN directory entries (32 bytes each), highest sequence first."""
    chksum = lfn_checksum(short_83)
    # UTF-16LE code units, NUL-terminated, padded with 0xFFFF to a multiple of 13
    units = [ord(c) for c in long_name] + [0x0000]
    while len(units) % 13 != 0:
        units.append(0xFFFF)
    chunks = [units[i:i+13] for i in range(0, len(units), 13)]
    n = len(chunks)
    entries = []
    for idx, chunk in enumerate(chunks):
        seq = idx + 1
        is_last = (idx == n - 1)
        seq_byte = seq | (0x40 if is_last else 0x00)
        e = bytearray(32)
        e[0] = seq_byte
        # name1: chars 0-4 (5 chars)
        for i in range(5):
            u = chunk[i]
            e[1 + i*2] = u & 0xFF
            e[1 + i*2 + 1] = (u >> 8) & 0xFF
        e[11] = 0x0F  # ATTR_LONG_NAME
        e[12] = 0x00  # type
        e[13] = chksum
        # name2: chars 5-10 (6 chars)
        for i in range(6):
            u = chunk[5 + i]
            e[14 + i*2] = u & 0xFF
            e[14 + i*2 + 1] = (u >> 8) & 0xFF
        e[26] = 0x00  # first cluster lo, always 0 for LFN entries
        e[27] = 0x00
        # name3: chars 11-12 (2 chars)
        for i in range(2):
            u = chunk[11 + i]
            e[28 + i*2] = u & 0xFF
            e[28 + i*2 + 1] = (u >> 8) & 0xFF
        entries.append(bytes(e))
    # LFN entries are stored highest-sequence-first in the directory
    entries.reverse()
    return entries

def put16(b, off, v):
    b[off] = v & 0xFF
    b[off+1] = (v >> 8) & 0xFF

def put32(b, off, v):
    b[off] = v & 0xFF
    b[off+1] = (v >> 8) & 0xFF
    b[off+2] = (v >> 16) & 0xFF
    b[off+3] = (v >> 24) & 0xFF

short83 = short_name_83_bytes()
lfns = lfn_entries(LONG_NAME, short83)

# --- layout: sector 0 = boot, sector 1 = FAT, sector 2 = root dir, sector 3+ = data
data_sectors_needed = (len(content) + SECTOR_SIZE - 1) // SECTOR_SIZE
DISK_SECTOR_COUNT = max(16, 3 + data_sectors_needed)  # >=16 sectors (8KB) for OS compat

disk = bytearray(DISK_SECTOR_COUNT * SECTOR_SIZE)

# Boot sector
boot = memoryview(disk)[0:SECTOR_SIZE]
boot[0:3] = bytes([0xEB, 0x3C, 0x90])
boot[3:11] = b"MSDOS5.0"
put16(boot, 11, SECTOR_SIZE)
boot[13] = 1                      # sectors per cluster
put16(boot, 14, 1)                # reserved sectors
boot[16] = 1                      # number of FATs
put16(boot, 17, 16)               # max root dir entries
put16(boot, 19, DISK_SECTOR_COUNT if DISK_SECTOR_COUNT < 0x10000 else 0)
boot[21] = 0xF8
put16(boot, 22, 1)                # sectors per FAT (only supports up to ~340 clusters; fine here)
put16(boot, 24, 1)
put16(boot, 26, 1)
put32(boot, 28, 0)
put32(boot, 32, 0)
boot[36] = 0x00
boot[37] = 0x00
boot[38] = 0x29
put32(boot, 39, 0x53424B54)
boot[43:54] = b"SWITCHBD   "
boot[54:62] = b"FAT12   "
boot[510] = 0x55
boot[511] = 0xAA

# FAT table (sector 1): cluster 2 = our one file, occupies data_sectors_needed clusters
fat = memoryview(disk)[SECTOR_SIZE:2*SECTOR_SIZE]
def fat_set12(fat_bytes, cluster, value):
    off = cluster + (cluster // 2)
    if cluster % 2 == 0:
        fat_bytes[off] = value & 0xFF
        fat_bytes[off+1] = (fat_bytes[off+1] & 0xF0) | ((value >> 8) & 0x0F)
    else:
        fat_bytes[off] = (fat_bytes[off] & 0x0F) | ((value & 0x0F) << 4)
        fat_bytes[off+1] = (value >> 4) & 0xFF

fat[0] = 0xF8
fat[1] = 0xFF
fat[2] = 0xFF
clusters = list(range(2, 2 + data_sectors_needed))
for i, c in enumerate(clusters):
    nxt = clusters[i+1] if i+1 < len(clusters) else 0xFFF
    fat_set12(fat, c, nxt)

# Root directory (sector 2): volume label, LFN entries, short entry
root = memoryview(disk)[2*SECTOR_SIZE:3*SECTOR_SIZE]
pos = 0
vol = bytearray(32)
vol[0:11] = b"SWITCHBD   "
vol[11] = 0x08
root[pos:pos+32] = vol
pos += 32

for e in lfns:
    root[pos:pos+32] = e
    pos += 32

short_entry = bytearray(32)
short_entry[0:11] = short83
short_entry[11] = 0x21  # ATTR_ARCHIVE | ATTR_READONLY
put16(short_entry, 26, 2)  # first cluster
put32(short_entry, 28, len(content))
root[pos:pos+32] = short_entry
pos += 32

assert pos <= SECTOR_SIZE, "root directory overflowed one sector"

# Data area starting at sector 3
data_start = 3 * SECTOR_SIZE
disk[data_start:data_start+len(content)] = content

with open("fat_image.bin", "wb") as f:
    f.write(disk)

with open("installer_disk.h", "w") as f:
    f.write("// Auto-generated by gen_fat.py — do not hand-edit.\n")
    f.write("// Regenerate with: python3 gen_fat.py 'Install Switchboard.command'\n")
    f.write("#pragma once\n#include <stdint.h>\n\n")
    f.write(f"static const uint32_t DISK_SECTOR_COUNT = {DISK_SECTOR_COUNT};\n")
    f.write(f"static const uint16_t DISK_SECTOR_SIZE = {SECTOR_SIZE};\n\n")
    f.write(f"static const uint8_t msc_disk_image[{DISK_SECTOR_COUNT * SECTOR_SIZE}] = {{\n")
    for i in range(0, len(disk), 16):
        row = disk[i:i+16]
        f.write("  " + ", ".join(f"0x{b:02X}" for b in row) + ",\n")
    f.write("};\n")

print(f"content bytes: {len(content)}")
print(f"data sectors needed: {data_sectors_needed}")
print(f"total disk sectors: {DISK_SECTOR_COUNT} ({DISK_SECTOR_COUNT*SECTOR_SIZE} bytes)")
print(f"LFN entries: {len(lfns)}")
print(f"root dir bytes used: {pos} / {SECTOR_SIZE}")
