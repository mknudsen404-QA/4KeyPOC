// Plain C++17, assert-based host test for the pure LED logic in
// ../led_model.h. No framework, no Arduino toolchain — see run.sh.
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include "../led_model.h"

static void expect_rgb_close(uint32_t actual, uint32_t expected, int tolerance, const char *label) {
  int ar = (actual >> 16) & 0xFF, ag = (actual >> 8) & 0xFF, ab = actual & 0xFF;
  int er = (expected >> 16) & 0xFF, eg = (expected >> 8) & 0xFF, eb = expected & 0xFF;
  bool ok = std::abs(ar - er) <= tolerance && std::abs(ag - eg) <= tolerance && std::abs(ab - eb) <= tolerance;
  if (!ok) {
    printf("%s: expected #%06X, got #%06X\n", label, expected, actual);
  }
  assert(ok);
}

static void test_hsv_to_rgb_reference() {
  expect_rgb_close(hsvToRgb(0.0f, 1.0f, 1.0f), 0xFF0000, 1, "hsv(0,1,1)");
  expect_rgb_close(hsvToRgb(120.0f, 1.0f, 1.0f), 0x00FF00, 1, "hsv(120,1,1)");
  expect_rgb_close(hsvToRgb(240.0f, 1.0f, 1.0f), 0x0000FF, 1, "hsv(240,1,1)");
  expect_rgb_close(hsvToRgb(210.0f, 0.0f, 0.55f), 0x8C8C8C, 1, "hsv(210,0,0.55)");
}

static void test_busy_color_endpoints() {
  expect_rgb_close(busyColor(0), SOFT_WHITE, 1, "busyColor(0)");
  expect_rgb_close(busyColor(BUSY_RAMP_MS), 0xFF00FF, 1, "busyColor(BUSY_RAMP_MS)");
}

static void test_busy_hue_stays_in_band() {
  for (uint32_t elapsed = 0; elapsed <= BUSY_RAMP_MS; elapsed += 1000) {
    float hue = busyHue(elapsed);
    assert(hue >= BUSY_START_HUE - 0.01f && hue <= BUSY_END_HUE + 0.01f);
  }
}

static void test_busy_color_monotonic() {
  float lastHue = -1.0f, lastSat = -1.0f;
  for (uint32_t elapsed = 0; elapsed <= BUSY_RAMP_MS; elapsed += 1000) {
    float hue = busyHue(elapsed);
    float sat = busySaturation(elapsed);
    assert(hue >= lastHue - 0.001f);
    assert(sat >= lastSat - 0.001f);
    lastHue = hue;
    lastSat = sat;
  }
}

static void test_status_from_string() {
  assert(statusIsBusy(statusFromString("working")));
  assert(statusIsBusy(statusFromString("thinking")));
  assert(!statusIsBusy(statusFromString("idle")));
  assert(statusFromString("garbage") == Status::Unknown);
}

static void test_render_key_dim_floor() {
  uint32_t color = renderKey(Status::Idle, 0, /*selected=*/false, 0);
  assert(color != 0);
}

static void test_pulse_periods() {
  assert(pulsePeriodFor(Status::NeedsInput, 0) == 500UL);
  assert(pulsePeriodFor(Status::Done, 0) == 0UL);
}

int main() {
  test_hsv_to_rgb_reference();
  test_busy_color_endpoints();
  test_busy_hue_stays_in_band();
  test_busy_color_monotonic();
  test_status_from_string();
  test_render_key_dim_floor();
  test_pulse_periods();
  printf("led_model_test: all tests passed\n");
  return 0;
}
