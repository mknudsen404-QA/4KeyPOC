#ifndef SWITCHBOARD_LED_MODEL_H
#define SWITCHBOARD_LED_MODEL_H

// Header-only, no Arduino includes: every pure function behind the LED
// rendering lives here so it can be built and tested with a plain host C++
// compiler (see test/run.sh) instead of only inside the Arduino toolchain.

#include <cstdint>
#include <cmath>
#include <cstring>

#include "status_table.h"  // Status, statusFromString, statusIsBusy, colorForStatus,
                            // staticPulseMsFor, SOFT_WHITE — generated, see that file's header comment

#ifndef PI
#define PI 3.14159265358979323846f
#endif

constexpr uint32_t BUSY_RAMP_MS = 300000;  // 5 min turn ramp — the built-in
                                            // default until a led.config
                                            // event overrides it (see
                                            // neokey.ino's busyRampMs global
                                            // and host/switchboard/settings.py's
                                            // LED_THINKING_CYCLE_PRESETS).
constexpr float BUSY_START_HUE = 210.0f;   // cool blue
constexpr float BUSY_END_HUE = 300.0f;     // magenta — never past 300 (into red)
constexpr float WHITE_PHASE = 1.0f / 3.0f; // first third of the ramp: white -> blue (saturation ramp)

// Standard HSV->RGB conversion (h in degrees 0-360, s/v in 0-1).
inline uint32_t hsvToRgb(float h, float s, float v) {
  float c = v * s;
  float x = c * (1.0f - fabsf(fmodf(h / 60.0f, 2.0f) - 1.0f));
  float m = v - c;
  float r = 0, g = 0, b = 0;
  if (h < 60.0f)       { r = c; g = x; b = 0; }
  else if (h < 120.0f) { r = x; g = c; b = 0; }
  else if (h < 180.0f) { r = 0; g = c; b = x; }
  else if (h < 240.0f) { r = 0; g = x; b = c; }
  else if (h < 300.0f) { r = x; g = 0; b = c; }
  else                 { r = c; g = 0; b = x; }
  return ((uint32_t)((r + m) * 255) << 16) | ((uint32_t)((g + m) * 255) << 8) | (uint32_t)((b + m) * 255);
}

// Smoothstep-eased progress (0..1) through the busy ramp. `rampMs` lets a
// runtime override (led.config, see neokey.ino) speed up or slow down the
// whole ramp — and, since busyPulsePeriodMs derives its pulse speed from
// this same progress, the pulse too — without changing its shape.
inline float busyRampProgress(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  if (rampMs == 0) rampMs = 1;  // guard against a zero override; never divide by zero
  uint32_t capped = elapsedMs < rampMs ? elapsedMs : rampMs;
  float t = (float)capped / (float)rampMs;
  return t * t * (3.0f - 2.0f * t);
}

// White -> blue (saturating) for the first third of the ramp, then blue ->
// magenta (full saturation, sweeping hue) for the rest. Never reaches red.
// Exposed separately (rather than folded straight into busyColor) so tests
// can check the hue/saturation the ramp actually chose without having to
// reverse-engineer them out of an RGB value.
inline float busyHue(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  float t = busyRampProgress(elapsedMs, rampMs);
  if (t < WHITE_PHASE) return BUSY_START_HUE;
  float u = (t - WHITE_PHASE) / (1.0f - WHITE_PHASE);
  return BUSY_START_HUE + (BUSY_END_HUE - BUSY_START_HUE) * u;
}

inline float busySaturation(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  float t = busyRampProgress(elapsedMs, rampMs);
  if (t < WHITE_PHASE) return t / WHITE_PHASE;
  return 1.0f;
}

inline float busyValue(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  float t = busyRampProgress(elapsedMs, rampMs);
  if (t < WHITE_PHASE) return 0.55f + 0.45f * (t / WHITE_PHASE);
  return 1.0f;
}

inline uint32_t busyColor(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  return hsvToRgb(busyHue(elapsedMs, rampMs), busySaturation(elapsedMs, rampMs), busyValue(elapsedMs, rampMs));
}

inline unsigned long busyPulsePeriodMs(uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS) {
  const unsigned long startMs = 4000UL;  // slow "giant heartbeat"
  const unsigned long endMs = 900UL;     // quick pulse
  float t = busyRampProgress(elapsedMs, rampMs);
  return startMs - (unsigned long)((startMs - endMs) * t);
}

// 0 means "solid" (no pulse) to the caller. The static (non-busy) periods
// come from status_table.h's generated staticPulseMsFor() UNLESS
// `idleOverrideMs` is nonzero and `s` is Idle, in which case that wins —
// a runtime led.config choice (e.g. a slow "breathing" idle) overriding
// the compiled-in default (solid) without needing a firmware rebuild. The
// busy ramp's own period (which needs elapsedMs, not a per-status
// constant) stays hand-written here.
inline unsigned long pulsePeriodFor(Status s, uint32_t elapsedMs, uint32_t rampMs = BUSY_RAMP_MS, unsigned long idleOverrideMs = 0) {
  if (statusIsBusy(s)) return busyPulsePeriodMs(elapsedMs, rampMs);
  if (s == Status::Idle && idleOverrideMs != 0) return idleOverrideMs;
  return staticPulseMsFor(s);
}

// Smooth raised-cosine breathing wave (0..1..0), floored at 0.35 so the pulse
// dims rather than blacks out completely.
inline float breathFactor(unsigned long nowMs, unsigned long periodMs) {
  if (periodMs == 0) return 1.0f;
  float phase = (float)(nowMs % periodMs) / (float)periodMs;
  float wave = 0.5f - 0.5f * cosf(phase * 2.0f * (float)PI);
  return 0.35f + 0.65f * wave;
}

inline uint32_t scaleColor(uint32_t color, float factor) {
  uint8_t r = (uint8_t)(((color >> 16) & 0xFF) * factor);
  uint8_t g = (uint8_t)(((color >> 8) & 0xFF) * factor);
  uint8_t b = (uint8_t)((color & 0xFF) * factor);
  return ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
}

// Dim a color when its key isn't the selected one, so selection is visible
// at a glance without needing a second LED per key. Floored per-channel so a
// dim (but nonzero) status never disappears entirely on a non-selected key.
inline uint32_t applySelectionFloor(uint32_t color, bool selected) {
  if (selected || color == 0) return color;
  uint8_t r = (uint8_t)(color >> 16);
  uint8_t g = (uint8_t)(color >> 8);
  uint8_t b = (uint8_t)color;
  auto dim = [](uint8_t channel) -> uint8_t {
    if (channel == 0) return 0;
    uint8_t scaled = (uint8_t)(channel * 0.40f);
    return scaled < 0x10 ? 0x10 : scaled;
  };
  r = dim(r);
  g = dim(g);
  b = dim(b);
  return ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
}

// The one function callers need per key, per frame: pick the base color
// (the busy ramp if the status is busy, else the static per-status color),
// dim it if this key isn't selected, then apply whatever pulse the status
// calls for.
//
// `uncertain` overrides all of that: liveness can no longer be confirmed
// for this slot (two consecutive UNKNOWN probes — see the reducer), so the
// LED may be lying about `s`. When true, render Status::Unknown's amber
// with its 3000ms pulse regardless of what `s` actually is.
//
// `rampMs` and `idleOverrideMs` are the two runtime LED-pulse settings
// (see led.config in neokey.ino): how long the busy color ramp takes, and
// whether Idle breathes instead of sitting solid. Both default to the
// firmware's original, hardcoded behaviour, so every existing call site
// (and every existing test) is unaffected until it opts in.
inline uint32_t renderKey(Status s, uint32_t elapsedMs, bool selected, unsigned long nowMs, bool uncertain = false, uint32_t rampMs = BUSY_RAMP_MS, unsigned long idleOverrideMs = 0) {
  if (uncertain) {
    uint32_t color = applySelectionFloor(colorForStatus(Status::Unknown), selected);
    return scaleColor(color, breathFactor(nowMs, pulsePeriodFor(Status::Unknown, elapsedMs, rampMs, idleOverrideMs)));
  }
  uint32_t base = statusIsBusy(s) ? busyColor(elapsedMs, rampMs) : colorForStatus(s);
  uint32_t color = applySelectionFloor(base, selected);
  unsigned long period = pulsePeriodFor(s, elapsedMs, rampMs, idleOverrideMs);
  if (period != 0) {
    color = scaleColor(color, breathFactor(nowMs, period));
  }
  return color;
}

#endif  // SWITCHBOARD_LED_MODEL_H
