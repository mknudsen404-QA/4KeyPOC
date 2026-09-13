#!/usr/bin/env bash
# Compile-only check (no upload). Needs: arduino-cli, esp32 core,
# "Adafruit seesaw Library", "ArduinoJson".
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc --warnings default .
