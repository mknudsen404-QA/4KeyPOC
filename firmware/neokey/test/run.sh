#!/usr/bin/env bash
# Builds and runs the host-side led_model.h test. Plain C++17, no framework,
# no Arduino toolchain. Arduino ignores this test/ subfolder when compiling
# the sketch.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
c++ -std=c++17 -I.. led_model_test.cpp -o /tmp/led_model_test
/tmp/led_model_test
