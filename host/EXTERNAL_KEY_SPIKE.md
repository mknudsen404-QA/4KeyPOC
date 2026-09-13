# External Key Switch Spike

This is the first physical-input test for Switchboard.

## Corrected actual-board plan

Use the board's UART connector area from the actual photos, not the I2C/PWM connectors and not an assumed camera/custom-knob connector.

The firmware maps External Agent Key 1 to `GPIO44`, which is the ESP32-S3 `RXD0` pin. RXD0 is an input, so it is the right kind of UART pin to repurpose for a simple switch test.

## Wiring

Wire a simple mechanical switch between `RXD0` and `GND`:

```text
RXD0  ---- switch ---- GND
```

Do not connect the switch to 5V. Do not use a TXD pin for this test. TXD pins are outputs.

The clearly labeled I2C connectors in the user's photo have pins labeled `SCL SDA GND 5V`; their `GND` pin may be used as ground if needed, but do not put the switch across I2C SCL/SDA.

## Expected behavior

With the bridge running:

```sh
python3 host/switchboard_bridge.py listen --port /dev/cu.usbmodem2301 --auto-launch
```

Pressing the external switch should:

1. Select Agent 1 on the board.
2. Emit:

   ```json
   {"event":"agent.select","slot":1}
   ```

3. Make the bridge launch/register Agent 1 if Agent 1 is empty.
4. Send Agent 1 status back to the board.

## Notes

- Stop the bridge before flashing firmware. Only one process can own the serial port.
- This is a one-switch proof. The final 4-agent + 6-command layout should use the NeoKey/Qwiic I2C key-bank path.
- The firmware pin is set in `main/APP/lv_mainstart.c` as `EXT_AGENT1_GPIO = GPIO_NUM_44`.
