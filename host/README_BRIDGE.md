# Switchboard Host Bridge

This is the first Mac-side bridge for Switchboard.

It now has two jobs:

1. Launch/register Codex or Claude CLI sessions into four stable agent slots.
2. Read JSON-lines events from the board, print what they mean, and push registered slot status back to the screen.

It is still observe-only for mutating actions: it does not approve plans, run slash commands, focus terminals, send text into agents, or start microphone recording yet. It also does not yet observe live Codex/Claude terminal output, so it cannot automatically know that a launched agent is asking for access or waiting for input.

The bridge must be running on the Mac for the hardware to control anything. The ESP32 board is only the controller; the Mac-side bridge is the driver/daemon that listens for button events and launches or controls agents.

## Voice hold (Claude-only, needs a venv)

`voice.hold.start`/`voice.hold.stop` drive Claude Code's `/voice` hold-to-record
mode for real: the bridge focuses the selected slot's Terminal tab, then posts
a real synthetic spacebar keyDown on hold-start and keyUp on hold-stop — a
true press-and-hold, not an atomic keystroke. This only works for
`claude`-family slots; Codex's `/voice` support (if any) isn't scoped yet.

**You must turn on `/voice` yourself, once per session, before using the PTT
key.** The bridge does not do this for you. An earlier version tried to
auto-detect and auto-toggle `/voice` by tailing the terminal's log for its
"Voice mode enabled."/"disabled." message, but that's unreliable against
Claude Code's TUI (it does full-screen ANSI redraws, not scrolling text, so a
byte-tail search can miss the toggle message) — it ended up silently
flipping voice mode the wrong way, and once even landed a `/voice` keystroke
as a real chat message mid-render (a real, billed turn). Simpler and safer:
you own `/voice`'s on/off state by hand; the bridge only ever sends the
hold/release keypresses once it's on.

This needs `pyobjc-framework-Quartz`, which Homebrew's system Python won't let
you `pip install` directly (PEP 668). Use the project-local venv instead:

```sh
cd host
python3 -m venv .venv
./.venv/bin/pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices
./.venv/bin/python3 switchboard_bridge.py listen --port /dev/cu.usbmodem2301 --auto-launch
```

Running the bridge with plain `python3` still works for everything else
(select/launch/status/sync) — voice hold just no-ops with a clear error
message ("Quartz is not installed...") until you run it with the venv's
Python instead.

The very first time you use voice hold, macOS will prompt for **Accessibility**
permission for whatever process is running the bridge (Terminal, or the venv's
python3) — this is required for `CGEventPostToPid` to work. Grant it once in
System Settings -> Privacy & Security -> Accessibility.

## Run with the board

From the firmware project folder:

```sh
python3 host/switchboard_bridge.py
```

If the board is on a known port:

```sh
python3 host/switchboard_bridge.py --port /dev/cu.usbmodem2301
```

The explicit subcommand is:

```sh
python3 host/switchboard_bridge.py listen --port /dev/cu.usbmodem2301
```

Auto-launch empty agent slots when their button event arrives:

```sh
python3 host/switchboard_bridge.py listen \
  --port /dev/cu.usbmodem2301 \
  --auto-launch
```

For development, register slots without opening Terminal windows:

```sh
python3 host/switchboard_bridge.py listen \
  --port /dev/cu.usbmodem2301 \
  --auto-launch \
  --no-open
```

## Test without the board

```sh
python3 host/switchboard_bridge.py --sample
```

## Launch one agent slot

This registers Agent 1 and opens a new macOS Terminal window running Codex in the firmware project folder:

```sh
python3 host/switchboard_bridge.py launch \
  --slot 1 \
  --name Maestro \
  --family codex \
  --cwd /Users/matthewknudsen/Documents/Codex-Micro-Matt/Codex_Micro_Display \
  --command codex
```

Preview without opening Terminal:

```sh
python3 host/switchboard_bridge.py launch \
  --slot 1 \
  --name Maestro \
  --command codex \
  --dry-run
```

Register without opening Terminal:

```sh
python3 host/switchboard_bridge.py launch \
  --slot 1 \
  --name Maestro \
  --command codex \
  --no-open
```

## Which CLI each agent key launches

Auto-launch and `launch-all` both read `host/agents.json` if it exists, falling
back to the checked-in `host/agents.example.json` template otherwise.
`agents.json` is yours — not checked in, safe to edit freely. Each slot is:

```json
{ "slot": 1, "name": "Maestro", "family": "codex", "cwd": "...", "command": "codex", "effort": "medium" }
```

Change `family` (`codex` or `claude`) and `command` per slot to control the
codex/claude mix — nothing stops all four slots from being the same family, or
all different. `effort` is optional (defaults to `medium`) and sets the
starting reasoning effort for that slot's session.

## Launch the four-slot example

The example config is `host/agents.example.json` — copy it to `host/agents.json`
to make it your own editable copy (already done once; see above).

```sh
python3 host/switchboard_bridge.py launch-all
```

The current example maps:

| Slot | Name | CLI |
| --- | --- | --- |
| 1 | Maestro | codex |
| 2 | Agent 2 | codex |
| 3 | Agent 3 | claude |
| 4 | Agent 4 | codex |

Preview the full launch set:

```sh
python3 host/switchboard_bridge.py launch-all --dry-run
```

List registered slots:

```sh
python3 host/switchboard_bridge.py slots
```

## Real status auto-detection (lifecycle hooks)

Run once to wire Switchboard into Claude Code's and Codex's real lifecycle hooks
(`SessionStart`, `UserPromptSubmit`, `Stop`, etc.) so status updates on its own
instead of only ever being set by hand:

```sh
python3 host/switchboard_bridge.py install-hooks
```

Safe to re-run any time (idempotent, merges into your existing
`~/.claude/settings.json` / `$CODEX_HOME/hooks.json` without touching unrelated
settings). Takes effect for sessions launched *after* this runs — an
already-open Terminal window won't pick up new hooks until restarted. See
"Status Auto-Detection Plan" in `CODEX_MICRO_CONSOLE_DESIGN.md` for how it works
and its known gaps (credit to [OpenMicro](https://github.com/stephenleo/OpenMicro)
for validating this approach).

## Track status

The bridge registry now stores status, effort, and activity per slot.

Mark Agent 2 as working:

```sh
python3 host/switchboard_bridge.py status \
  --slot 2 \
  --status working \
  --activity "running tests"
```

Mark Agent 2 done:

```sh
python3 host/switchboard_bridge.py status --slot 2 --status done
```

Clear Agent 2:

```sh
python3 host/switchboard_bridge.py clear --slot 2
```

Push registered status back to the board:

```sh
python3 host/switchboard_bridge.py sync --port /dev/cu.usbmodem2301
```

Push one slot and wait for the board to confirm the update:

```sh
python3 host/switchboard_bridge.py sync --port /dev/cu.usbmodem2301 --slot 1
```

The shorter form also works:

```sh
python3 host/switchboard_bridge.py sync --port /dev/cu.usbmodem2301 1
```

A healthy round trip prints:

```text
Sent Agent 1 status to board
Board applied update for Agent 1
```

Current statuses:

```text
empty
launched
idle
thinking
working
waiting
needs_input
blocked
done
```

## External key switch spike

The current firmware has one direct external switch input enabled:

```text
GPIO44 (RXD0) ---- switch ---- GND
```

GPIO44 acts as External Agent Key 1. It is separate from the onboard KEY1-KEY4 buttons.

See `host/EXTERNAL_KEY_SPIKE.md`.

## Install as a login background bridge

For a real user-facing setup, install the bridge as a macOS LaunchAgent. That makes it start at login and keep waiting for the board if the board is not plugged in yet.

Preview the install:

```sh
python3 host/install_bridge_launch_agent.py --dry-run
```

Install and start it:

```sh
python3 host/install_bridge_launch_agent.py
```

Development install that does not open Terminal windows:

```sh
python3 host/install_bridge_launch_agent.py --no-open
```

The LaunchAgent writes:

```text
~/Library/LaunchAgents/com.switchboard.bridge.plist
~/Library/Logs/Switchboard/bridge.out.log
~/Library/Logs/Switchboard/bridge.err.log
```

The installed bridge runs with `--auto-launch --retry`. It waits for the board, then launches/registers an empty agent slot when an agent button event arrives.

## CLI discovery

The bridge resolves `codex` and `claude` in this order:

1. The current PATH.
2. Known local app/binary locations.
3. An absolute path passed through `--command`.

On Matthew's Mac, Codex resolves to:

```text
/Applications/ChatGPT.app/Contents/Resources/codex
```

Claude resolves to:

```text
/Users/matthewknudsen/.local/bin/claude
```

## Current firmware events

```json
{"event":"agent.select","slot":1}
{"event":"agent.focus","slot":1}
{"event":"agent.reasoning.apply","slot":1,"effort":"HIGH"}
{"event":"voice.hold.start","slot":1}
{"event":"voice.hold.stop","slot":1}
```

`agent.select` now also brings that slot's Terminal window to front if it
already has a live session (confirmed working) — not just on first launch.
`agent.focus` does the same thing without changing the selected slot, for a
future case where those two need to be separate actions.

## Intended future events

```json
{"event":"plan.approve","slot":1}
{"event":"review.request","slot":1}
{"event":"slash.run","slot":1,"command":"/review"}
```

## TODO: token-consumption LED pattern

Idea, not yet designed or implemented: once `busy_seconds`-based duration
escalation (below) is working, layer in something similar driven by real
token usage per slot — e.g. the LED pattern gets more intense/urgent the more
tokens a turn has burned, not just how long it's taken. Needs the hook
payload body to actually be parsed (today `do_POST` drains and discards it)
and `transcript_path` read for per-turn `usage` data — a real lift, not a
quick add. Revisit after duration escalation is proven out.

## Testing status auto-detection without spending tokens

The bridge tees every launched session's raw output to `host/logs/slot-N.log`
(via `script -q`) and a background thread tails it, pattern-matching for
status changes — see "Status Auto-Detection Plan" in
`CODEX_MICRO_CONSOLE_DESIGN.md`. To test that whole pipeline — log → poll
thread → registry → serial push → screen — without launching a real codex/claude
session:

```sh
# 1. Launch a harmless test agent (cat echoes back whatever it's given —
#    exactly what the tee/poll pipeline needs, at zero API cost).
python3 host/switchboard_bridge.py launch --slot 1 --name Test --family shell --command cat

# 2. With `listen` running (auto-launch or not, doesn't matter here), inject
#    trigger text into that slot's real terminal tab:
python3 host/switchboard_bridge.py test-trigger --slot 1 "waiting for approval (y/n)"
python3 host/switchboard_bridge.py test-trigger --slot 1 "Traceback (most recent call last):"

# 3. Check the registry picked it up (give the poller ~2s):
python3 host/switchboard_bridge.py slots

# 4. Watch the board — Operator 1 should flip status color/word live.

# 5. Clean up when done:
python3 host/switchboard_bridge.py clear --slot 1
```

`test-trigger` refuses to run against a non-`shell` slot (`--force` overrides)
since typing into a real agent's terminal would be typing into its actual
input, not a safe test. Current detection patterns are intentionally minimal
and unverified against real codex/claude output (see the design doc) — this
harness is exactly how to safely find and add better ones without spending
real usage on the process of finding them.

## Safety rule

Mutating actions stay inert until the bridge has an explicit implementation and a deliberate confirmation path. The firmware can emit a requested action, but this bridge only reports it.
