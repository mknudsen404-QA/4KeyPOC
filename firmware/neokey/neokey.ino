#include <Wire.h>
#include <math.h>
#include <ArduinoJson.h>
#include "Adafruit_NeoKey_1x4.h"
#include "seesaw_neopixel.h"
#include "led_model.h"

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

int selectedSlot = 0;  // 0 = none selected yet
Status slotStatus[AGENT_KEY_COUNT] = {Status::Empty, Status::Empty, Status::Empty};
unsigned long slotBusyStartMs[AGENT_KEY_COUNT] = {0, 0, 0};
// Set from each update's "liveness" field: "unknown" -> true, "alive" or
// the field being absent (a status-driven update, e.g. from a hook, which
// only happens if the process is alive) -> false.
bool slotUncertain[AGENT_KEY_COUNT] = {false, false, false};
bool ptt_held = false;

void redrawAgentKeys() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
    bool isSelected = (selectedSlot == AGENT_SLOTS[i]);
    uint32_t elapsed = statusIsBusy(slotStatus[i]) ? (uint32_t)(now - slotBusyStartMs[i]) : 0;
    neokey.pixels.setPixelColor(i, renderKey(slotStatus[i], elapsed, isSelected, now, slotUncertain[i]));
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
  static char lineBuf[384];
  static size_t lineLen = 0;

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) {
        StaticJsonDocument<384> doc;
        DeserializationError err = deserializeJson(doc, lineBuf);
        if (!err) {
          const char *eventName = doc["event"] | "";
          if (strcmp(eventName, "agent.update") == 0) {
            int slot = doc["slot"] | 0;
            const char *status = doc["status"] | "empty";
            // busy_seconds is accepted for one release as a fallback while
            // any bridge that hasn't been restarted yet is still sending it.
            uint32_t busyElapsedMs = doc["busy_elapsed_ms"] | (uint32_t)(doc["busy_seconds"] | 0) * 1000UL;
            bool uncertain = strcmp(doc["liveness"] | "", "unknown") == 0;
            for (uint8_t i = 0; i < AGENT_KEY_COUNT; i++) {
              if (AGENT_SLOTS[i] == slot) {
                slotStatus[i] = statusFromString(status);
                slotBusyStartMs[i] = millis() - busyElapsedMs;
                slotUncertain[i] = uncertain;
              }
            }
            sendEvent("agent.update.ack", slot);
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
