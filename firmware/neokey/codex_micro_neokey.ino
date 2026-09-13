#include <Wire.h>
#include <math.h>
#include <ArduinoJson.h>
#include "Adafruit_NeoKey_1x4.h"
#include "seesaw_neopixel.h"

#define NEOKEY_ADDR 0x30
#define SDA_PIN 9
#define SCL_PIN 8

// Keys A, B, C (indices 0-2) are agent-select keys mapped to Switchboard
// slots 1-2-3. Key D (index 3) is push-to-talk for whichever slot is
// currently selected.
#define AGENT_KEY_COUNT 3
#define PTT_KEY_INDEX 3
const uint8_t AGENT_SLOTS[AGENT_KEY_COUNT] = {1, 2, 3};

Adafruit_NeoKey_1x4 neokey;

// Busy ("working"/"thinking") slots don't use a fixed color — they sweep
// continuously from a cool blue (just started) to a warm ember orange (been
// running a while), with the breathing pulse quickening alongside it, over
// this many seconds. Past this point the color/pulse holds at the ember end
// rather than continuing to climb.
//
// The hue sweep deliberately goes "the long way" around the color wheel —
// up through purple/magenta — rather than the shorter way down through
// cyan/green/yellow, because the latter passes directly through the exact
// hues used by "done" (green) and "waiting"/"needs_input" (yellow), which
// made a mid-ramp busy key briefly look like a different status entirely.
// BUSY_END_HUE_DEG is intentionally > 360; busyHueDegrees() wraps it back
// into range after interpolating, so it still lands on ember orange (20°).
#define BUSY_RAMP_END_SEC 300
#define BUSY_START_HUE_DEG 210.0f   // cool blue
#define BUSY_END_HUE_DEG 380.0f     // wraps to 20° (ember orange) via purple/magenta
#define BUSY_START_PULSE_MS 4000UL  // slow "giant heartbeat"
#define BUSY_END_PULSE_MS 900UL     // quick pulse

int selectedSlot = 0;          // 0 = none selected yet
uint32_t slotColor[AGENT_KEY_COUNT] = {0, 0, 0};      // last agent.update color per agent key
uint32_t slotBusySeconds[AGENT_KEY_COUNT] = {0, 0, 0};  // how long that slot has been busy
bool slotIsBusy[AGENT_KEY_COUNT] = {false, false, false};  // status is "working" or "thinking"
bool ptt_held = false;

uint32_t colorForStatus(const char *status) {
  // Red is reserved for "blocked" (a real error/traceback) — everything
  // else, including needs_input, is routine, not a problem. needs_input in
  // particular covers benign things like Claude Code's first-run "do you
  // trust this folder?" prompt, which isn't an error and shouldn't look
  // like one. "blocked" is currently unreachable (no live signal sets it,
  // see host/switchboard_bridge.py) but stays defined for when one does.
  // "working"/"thinking" are handled dynamically in redrawAgentKeys() and
  // never read from here.
  if (strcmp(status, "done") == 0) return 0x00FF00;         // green
  if (strcmp(status, "waiting") == 0) return 0xFFFF00;      // yellow: wants attention, not urgent
  if (strcmp(status, "needs_input") == 0) return 0xFFFF00;  // yellow: wants attention, not urgent
  if (strcmp(status, "blocked") == 0) return 0xFF0000;      // red: something's actually wrong
  if (strcmp(status, "launched") == 0) return 0x0000FF;     // blue
  if (strcmp(status, "idle") == 0) return 0x202020;         // dim white
  return 0;  // empty / unknown = off
}

// Standard HSV->RGB conversion (h in degrees 0-360, s/v in 0-1), so busy
// colors can sweep smoothly through hue instead of jumping between fixed
// presets.
uint32_t hsvToRgb(float h, float s, float v) {
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

// Smoothstep-eased progress (0..1) through the busy ramp: stays near 0 early,
// near 1 late, with the crossfade happening around the midpoint — this is
// what keeps the color/pulse flat at each end instead of ramping the instant
// a turn starts.
float busyRampProgress(uint32_t busySeconds) {
  float t = (float)min(busySeconds, (uint32_t)BUSY_RAMP_END_SEC) / (float)BUSY_RAMP_END_SEC;
  return t * t * (3.0f - 2.0f * t);
}

float busyHueDegrees(uint32_t busySeconds) {
  float t = busyRampProgress(busySeconds);
  float hue = BUSY_START_HUE_DEG + (BUSY_END_HUE_DEG - BUSY_START_HUE_DEG) * t;
  if (hue >= 360.0f) hue -= 360.0f;
  return hue;
}

unsigned long busyPulsePeriodMs(uint32_t busySeconds) {
  float t = busyRampProgress(busySeconds);
  return BUSY_START_PULSE_MS - (unsigned long)((BUSY_START_PULSE_MS - BUSY_END_PULSE_MS) * t);
}

// Dim a color when its key isn't the selected one, so selection is visible
// at a glance without needing a second LED per key.
uint32_t applySelection(uint32_t color, bool selected) {
  if (selected || color == 0) return color;
  uint8_t r = (uint8_t)(color >> 16);
  uint8_t g = (uint8_t)(color >> 8);
  uint8_t b = (uint8_t)color;
  return ((uint32_t)(r / 4) << 16) | ((uint32_t)(g / 4) << 8) | (uint32_t)(b / 4);
}

// Smooth raised-cosine breathing wave (0..1..0), floored at 0.35 so the pulse
// dims rather than blacks out completely (staying legible even at its
// darkest point). A cosine ease reads as an organic "breath"/"heartbeat"
// rather than the mechanical blink of a linear triangle wave.
float breathFactor(unsigned long nowMs, unsigned long periodMs) {
  float phase = (float)(nowMs % periodMs) / (float)periodMs;
  float wave = 0.5f - 0.5f * cosf(phase * 2.0f * (float)PI);
  return 0.35f + 0.65f * wave;
}

uint32_t scaleColor(uint32_t color, float factor) {
  uint8_t r = (uint8_t)(((color >> 16) & 0xFF) * factor);
  uint8_t g = (uint8_t)(((color >> 8) & 0xFF) * factor);
  uint8_t b = (uint8_t)((color & 0xFF) * factor);
  return ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
}

void redrawAgentKeys() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
    bool isSelected = (selectedSlot == AGENT_SLOTS[i]);
    uint32_t baseColor = slotIsBusy[i]
        ? hsvToRgb(busyHueDegrees(slotBusySeconds[i]), 1.0f, 1.0f)
        : slotColor[i];
    uint32_t color = applySelection(baseColor, isSelected);
    if (slotIsBusy[i]) {
      color = scaleColor(color, breathFactor(now, busyPulsePeriodMs(slotBusySeconds[i])));
    }
    neokey.pixels.setPixelColor(i, color);
  }
}

void sendEvent(const char *eventName, int slot) {
  StaticJsonDocument<128> doc;
  doc["event"] = eventName;
  doc["slot"] = slot;
  serializeJson(doc, Serial);
  Serial.println();
}

// Non-blocking line reader for incoming bridge JSON (agent.update events).
void pollIncomingSerial() {
  static char lineBuf[256];
  static size_t lineLen = 0;

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) {
        StaticJsonDocument<256> doc;
        DeserializationError err = deserializeJson(doc, lineBuf);
        if (!err) {
          const char *eventName = doc["event"] | "";
          if (strcmp(eventName, "agent.update") == 0) {
            int slot = doc["slot"] | 0;
            const char *status = doc["status"] | "empty";
            uint32_t busySeconds = doc["busy_seconds"] | 0;
            bool isBusy = strcmp(status, "working") == 0 || strcmp(status, "thinking") == 0;
            for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
              if (AGENT_SLOTS[i] == slot) {
                slotColor[i] = colorForStatus(status);
                slotBusySeconds[i] = busySeconds;
                slotIsBusy[i] = isBusy;
              }
            }
          }
        }
      }
      lineLen = 0;
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(2000);

  Wire.begin(SDA_PIN, SCL_PIN);

  // NOTE: we deliberately do NOT call neokey.begin() here. That function
  // issues a seesaw software-reset (SWRST) before doing anything else, and on
  // this board that reset reliably leaves the chip unable to answer any
  // further register reads (confirmed with a raw I2C diagnostic sketch) even
  // though plain reads/writes work fine otherwise. So we replicate begin()'s
  // steps manually, skipping the reset call.
  bool ok = false;
  while (!ok) {
    ok = neokey.pixels.Adafruit_seesaw::begin(NEOKEY_ADDR, -1, false) &&
         neokey.Adafruit_seesaw::begin(NEOKEY_ADDR, -1, false);
    if (!ok) {
      delay(1000);
    }
  }

  neokey.pixels.updateType(NEO_GRB + NEO_KHZ800);
  neokey.pixels.updateLength(4);
  neokey.pixels.setPin(NEOKEY_1X4_NEOPIN);
  neokey.pixels.setBrightness(40);
  neokey.pixels.show();
  delay(5);

  neokey.pinModeBulk(NEOKEY_1X4_BUTTONMASK, INPUT_PULLUP);
  neokey.setGPIOInterrupts(NEOKEY_1X4_BUTTONMASK, 1);

  // Startup light sweep so you get visual confirmation without the monitor open
  for (uint16_t i = 0; i < neokey.pixels.numPixels(); i++) {
    neokey.pixels.setPixelColor(i, 0x808080);
    neokey.pixels.show();
    delay(80);
  }
  for (uint16_t i = 0; i < neokey.pixels.numPixels(); i++) {
    neokey.pixels.setPixelColor(i, 0);
    neokey.pixels.show();
    delay(80);
  }
}

uint8_t lastButtons = 0;

void loop() {
  pollIncomingSerial();

  uint8_t buttons = neokey.read();
  uint8_t pressedEdge = buttons & ~lastButtons;   // bits that just went from 0->1
  uint8_t releasedEdge = ~buttons & lastButtons;  // bits that just went from 1->0
  lastButtons = buttons;

  for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
    if (pressedEdge & (1 << i)) {
      selectedSlot = AGENT_SLOTS[i];
      sendEvent("agent.select", selectedSlot);
    }
  }

  if (pressedEdge & (1 << PTT_KEY_INDEX)) {
    ptt_held = true;
    sendEvent("voice.hold.start", selectedSlot);
  }
  if (releasedEdge & (1 << PTT_KEY_INDEX)) {
    ptt_held = false;
    sendEvent("voice.hold.stop", selectedSlot);
  }

  redrawAgentKeys();
  neokey.pixels.setPixelColor(PTT_KEY_INDEX, ptt_held ? 0xFF00FF : 0);
  neokey.pixels.show();

  delay(10);
}
