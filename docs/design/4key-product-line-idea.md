# 4Key product line + build-experience idea

Raw idea log — captured 2026-09-12, not yet a plan. For business-plan drafting, see (once written) `4key-business-plan.md` in this same folder.

## The product line

The current 4Key POC (`firmware/neokey/`, `host/switchboard_bridge.py`) is the entry point of a tiered lineup, scaling by number of agent-slot keys:

- **4Key** — entry model. 3 agent-select keys + 1 push-to-talk key (current POC).
- **8Key** — mid-tier, more concurrent agent slots.
- **12Key** (or similar) — top tier, for people running a lot of parallel agents/sessions.

Exact key counts/mappings per tier still TBD — the current AGENT_KEY_COUNT/PTT split would need to generalize.

## The buying experience

Not just "order a keypad" — a build-to-order, watch-it-happen experience:

- Customer picks their case color and features at checkout (like a custom PC build or a sneaker configurator).
- Customer can then **watch the build happen** — live or recorded — 3D printing the case, then the board/electronics getting dropped into the printed case, assembled, tested, and shipped.
- The idea is the fabrication itself is part of the product experience, not hidden factory work — closer to how some watch/knife/keyboard makers show the build process, but tied to an actual live or near-live feed per order rather than generic marketing video.

## Why this might be interesting (unrefined)

- The underlying tech (ESP32-S3 + NeoKey + Switchboard bridge) is already proven at the 4-key scale.
- "Watch your literal unit get made" is a differentiator mechanical-keyboard/artisan-keycap communities already respond to — there's a precedent for people paying a premium for that transparency/ritual.
- Tiered lineup (4/8/12) gives a natural upsell ladder instead of one SKU.

## Naming brainstorm

Riffing on "Claude doesn't want the carrot" — i.e. the product is a stick to keep agents in line — as a way to get a fun brand name *without* using "Claude"/"Codex" in the product name (see trademark risk in the business plan). Not settled, just capturing the thread:

- "The Stick" (entry idea, 4Key)
- "The Whip", "The Overseer", "The Foreman" (monitoring/wrangling angle)
- Tier-ladder naming logic: e.g. 4Key = "The Stick", 8Key = "The Switch", 12Key = "The Carrot" (top tier finally "rewards" you) — playing on the carrot-and-stick idiom across the whole lineup.

## Open questions (not yet answered)

- Is the "watch your build" feature live-streamed per order, a recorded video sent after, or an in-person/showroom experience? Big difference in cost/complexity.
- Manufacturing model: home 3D printer(s) run by the user personally (slow, low volume, high authenticity) vs. contracted print farm (scales, loses the personal-build narrative) vs. hybrid.
- Software/bridge side (`host/switchboard_bridge.py`) currently assumes Claude Code / Codex CLI hooks on a Mac — licensing, support burden, and hardware-vs-software split of the business need thought.
- Pricing ladder across 4/8/12 tiers, and whether the "build experience" is a paid add-on or baked into every order.
- This is explicitly meant to start as a spare-time side project with room to ramp up — plan should reflect that pace, not a funded startup launch.
