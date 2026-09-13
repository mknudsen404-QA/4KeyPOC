# 4Key / Switchboard — Business Plan (spare-time, bootstrapped)

Drafted 2026-09-12. Turns `docs/design/4key-product-line-idea.md` into an actionable
plan. Calibrated to **one person, a few hours a week, no outside money** — not a
funded launch.

Every factual claim about what exists today is sourced from this repo
(`firmware/neokey/`, `host/switchboard_bridge.py`, `CODEX_MICRO_CONSOLE_DESIGN.md`,
`README_CODEX_MICRO.md`, `host/README_BRIDGE.md`, `docs/design/plug-and-play-installer-plan.md`).
Every number that is *not* from the repo — component prices, market size, conversion
rates — is an estimate, and is labeled as one. There is no sales data yet, because
nothing has ever been sold.

---

## 1. Honest starting position

**What actually works today, on real hardware:**

- A 4-key NeoKey 1x4 + ESP32-S3 unit whose keys light per agent slot:
  3 agent-select keys + 1 push-to-talk, `AGENT_KEY_COUNT 3` / `PTT_KEY_INDEX 3`
  (`firmware/neokey/codex_micro_neokey.ino`).
- Real lifecycle-hook status detection, not screen-scraping: `install-hooks` merges
  marker-guarded hook commands into `~/.claude/settings.json` and `$CODEX_HOME/hooks.json`,
  each session gets `SWITCHBOARD_SLOT=N`, and a local `ThreadingHTTPServer` on
  `127.0.0.1:8877` maps `(family, event)` → status (`CLAUDE_HOOK_STATUS` /
  `CODEX_HOOK_STATUS` in `host/switchboard_bridge.py`).
- A duration-based busy animation that is genuinely good product design: busy keys
  sweep hue 210° → 380°/20° over `BUSY_RAMP_END_SEC 300` with the breath period
  tightening 4000 ms → 900 ms, deliberately routed through purple/magenta so a
  mid-ramp busy key never masquerades as `done` (green) or `waiting` (yellow).
  Validated on hardware (commit `fced269`).
- A one-command install on a fresh Mac (`host/setup.sh`: venv, hooks, login
  LaunchAgent) — genuinely better onboarding than most hobby hardware.
- Working push-to-talk into Claude Code's `/voice` via real synthetic keyDown/keyUp.

**What does not work yet, and matters commercially:**

- `blocked` (red) is **unreachable**. The firmware comment says so outright, and
  `switchboard_bridge.py` explains why: Codex has no error hook, Claude's
  `PreToolUse` only fires for `AskUserQuestion`, and the text-scan fallback was
  flipping keys red during ordinary subagent runs. "See instantly when an agent is
  stuck" is the most saleable line in the pitch and it is the one feature that
  currently does not exist. Do not market it until it does.
- Mutating actions are observe-only: no plan approval, no slash-command send, no
  terminal focus (`host/README_BRIDGE.md`). The Commands layer does not exist in
  firmware at all.
- `LIVE_EFFORT_RESTART_ENABLED = False`, disabled because failed Ctrl-C interrupts
  typed a shell command into a live conversation.
- One validated unit. No case, no enclosure design, no repeatable build process.
  The firmware README documents that this specific board could not survive a seesaw
  software reset and needed a hand-rolled `begin()` — i.e. per-unit silicon quirks
  are already proven to exist, which is the opposite of a manufacturing process.
- Mac-only. The bridge assumes macOS paths, `launchctl`, and Quartz synthetic input.

The gap between "impressive personal project" and "product" is mostly the second
list plus an enclosure. That is a real amount of work, and the plan below spends
Phase 0 and 1 on it rather than pretending it's done.

---

## 2. Market and customer

### Who actually buys this

**Tier 1 — the real customer (est. 70% of sales).** Developers running 3+
concurrent Claude Code / Codex CLI sessions who have physically lost track of which
terminal wants them. This person tab-cycles between panes to check if an agent is
done, and has already improvised something — a terminal-bell hook, a Slack ping, a
tmux status line. They are buying *peripheral vision*, not a keypad. They skew
senior, remote, and already own a mechanical keyboard and a nice desk. Price
sensitivity is low; patience for fiddliness is also low.

**Tier 2 — desk-setup and macropad enthusiasts (est. 20%).** r/MechanicalKeyboards,
r/olkb, r/battlestations. They will buy for the object and the RGB behavior and may
never run a second agent session. They are the most demanding on switch feel,
keycap quality, case finish, and photography — and the most likely to publicly
critique a 3D-printed case with visible layer lines. Valuable as amplifiers.

**Tier 3 — streamers, YouTubers, AI-dev content creators (est. 10% of units,
disproportionate share of reach).** For them the breathing hue ramp *is* the
content: it is a visually legible, camera-friendly readout of something otherwise
invisible. A handful of units placed here is the cheapest marketing available.
Consider gifting rather than selling.

**Who does not buy this:** anyone running one agent at a time (the majority), IDE/GUI
users (Cursor, the Claude desktop app) since the whole status pipeline is hooked into
CLI lifecycle events, Linux/Windows users until the bridge is ported, and anyone who
would rather spend 30 minutes wiring a $20 keypad themselves — which brings us to the
uncomfortable part.

### How niche this is, plainly

Two compounding constraints:

1. **The software half is free and already exists.** `CODEX_MICRO_CONSOLE_DESIGN.md`
   cites [OpenMicro](https://github.com/stephenleo/OpenMicro) (128 stars) as prior
   art solving the same problem with game controllers, and it is the reference
   implementation for this repo's own hook installer. This repo is *also* public
   (`github.com/mknudsen404-QA/4KeyPOC`, made public deliberately per
   `docs/design/plug-and-play-installer-plan.md`). So the function is a free
   download. The product is the object.
2. **The addressable population is a fraction of a fraction.** Plausible order of
   magnitude, estimated, not sourced: single-digit millions of people use an AI
   coding CLI; maybe 1-3% habitually run three or more sessions in parallel;
   of those, the fraction who buy a $150-350 single-purpose desk ornament is — by
   analogy to artisan keycap and small-batch macropad drops — well under 1%.

That lands at a realistic **lifetime** ceiling in the hundreds to low thousands of
units, and a realistic **year-one** volume of **20-80 units** for a solo operator
with no ad budget. Plan the business around that number. It is a good side income
and a bad startup.

The upside case is not "capture a % of developers." It is: this is a distinctive
object in a category (AI-agent desk hardware) that barely exists in 2026, and being
early with something photogenic can produce a single viral video that sells out a
year of spare-time capacity. Build for that asymmetry — keep capacity flexible, keep
a waitlist, never pre-sell more than you can build.

---

## 3. Product ladder

Differentiation by key count is the right spine — it maps to a real user variable
(how many parallel agents) rather than to artificial feature gating. But key count
alone won't justify a 2.3x price spread, so each tier adds one genuine capability.

| | **4Key** | **8Key** | **12Key** |
|---|---|---|---|
| Position | Entry / "does one thing" | The volume seller | Flagship |
| Keys | 4 (3 agents + PTT) | 8 (6 agents + PTT + cancel) | 12 (6 agents + 6 command keys) |
| Layout | 1× NeoKey 1x4 | 2× NeoKey 1x4 | 3× NeoKey 1x4, 4 cols × 3 rows (already locked in `CODEX_MICRO_CONSOLE_DESIGN.md`) |
| Screen | none | none | 2.0" ST7789 320×240 in its own cradle |
| Rotary dial | no | no | yes (reasoning-effort dial) |
| Command keys | no | 1 (cancel) | 6 (approve / review / LGTM / mic / slash / back) |
| Case | single-color | single-color + accent | two-tone, optional diffuser plate |
| **Est. BOM** | **~$45** | **~$68** | **~$110** |
| **Price** | **$149** | **$229** | **$349** |
| Gross margin | ~$104 (70%) | ~$161 (70%) | ~$239 (68%) |

**4Key BOM detail (estimated at single-unit retail, US, 2026):** ESP32-S3 dev board
$10-14 · Adafruit NeoKey 1x4 QT ~$13 · 4× Gateron MX switches ~$2 (36 already on
hand per the locked parts list) · 4 translucent keycaps ~$3 · STEMMA QT + USB-C
cable $5 · printed case, ~40 g PETG, with print-failure and printer amortization
~$4 · packaging $5 · fasteners/heat-set inserts/feet $3. **≈ $45.** Buying switches,
caps, and cables in 50-100 unit lots plausibly takes this to $36-40.

8Key adds a second NeoKey (+$13), 4 more switches/caps (+$5), a larger case (+$3),
and one command key's worth of firmware work. 12Key adds a third NeoKey (+$13), the
ST7789 panel ($12-18), a KY-040 encoder ($3), and roughly double the case material
and print time.

**The pricing logic.** ~3x BOM is the floor for hardware you personally support:
payment fees take ~3%, domestic shipping $10-15 (charge it separately), and a single
RMA or a two-hour support thread eats an entire unit's margin. At $149 the 4Key sits
just under the Stream Deck MK.2 psychological anchor, which is exactly where a
buyer's brain files "specialized desk peripheral, not a toy." Do not go below $129 —
a cheap price on a hand-built object signals mass production and invites mass-production
expectations.

**The bet: 8Key is the volume seller, not 4Key.** 4Key exists to establish a price
floor and let people say yes cheaply; 12Key exists to make 8Key look reasonable.
Expect roughly 25/50/25 unit mix.

**Cost lever already scoped.** `CODEX_MICRO_CONSOLE_DESIGN.md` costs the V2 custom
PCB (XL9555/PCF8575 + one WS2812 chain) at ~$5-10 per unit versus ~$18-24 for three
NeoKey boards, and notes the 5-board fab minimum makes it *more* expensive for a
single unit. On the 12Key that is ~$14-19 of margin per unit, plus it removes the
sourcing single point of failure in §5. It only pays off past ~15-20 units of a
tier. Don't start it before Phase 2.

---

## 4. The build-experience differentiator

The idea: customer configures color/features at checkout, then watches *their* unit
get printed and assembled. Three possible implementations, assessed against "one
person, a few hours a week."

**Live per-order streaming — not viable. Do not build this.**
- A case print is 3-5 hours of a nozzle moving very slowly. There is no version of
  this that a customer watches live. The interesting part (assembly, flashing, the
  first key lighting up) is 20 minutes and would need to be scheduled with the
  customer across time zones, on an evening, with a camera rig and lighting already
  set up and working.
- It converts every order into a calendar appointment. At three orders a week that is
  the entire spare-time budget spent performing rather than building.
- It exposes failed prints, stringing, a hand-soldering slip, and — given the firmware
  README's documented seesaw-reset quirk — the real possibility of live-debugging I2C
  in front of a paying customer.
- It also broadcasts the inside of a private home. Non-trivial, and irreversible.

**In-person / showroom — not viable at this scale.** Requires space, insurance,
scheduled foot traffic, and it geographically restricts the customer base to one
city, discarding the only genuine advantage of a niche internet product: the whole
internet is the catchment area. Revisit only as a booth at one keyboard meetup or an
AI-dev conference, as marketing rather than as fulfillment.

**Batch-recorded, per-unit-identified video — viable. Recommended.**

The insight is that customers do not want *live*; they want *proof it was theirs*.
Artisan keyboard and knife makers deliver exactly that with edited, asynchronous
content. Concretely:

1. **Print timelapse, effectively free.** Every modern slicer/printer ecosystem
   (Octolapse, Bambu Studio, Obico) produces a per-layer timelapse with no operator
   time. Batch 4-6 cases per plate; one timelapse covers the batch.
2. **Per-unit hero moment, ~5 minutes.** One phone clip: the customer's serial
   number written on a card next to *their* case, boards going in, USB plugged in,
   the startup light sweep running (`setup()` already does a deliberate white sweep
   in `codex_micro_neokey.ino` — that is the money shot, and it exists already), then
   a live agent turn breathing blue→ember.
3. **Delivery:** private unlisted link + QR code on a numbered card in the box.
   ~10-15 minutes of editing per unit, batched on one evening a week.

**Marginal cost: ~15 min/unit. Marginal price support: plausibly $20-40 of
willingness-to-pay, and more importantly it is the reason someone posts about the
purchase** — which is the actual return.

**Where it breaks down:** past roughly 20-25 units/month the per-unit filming and
editing collide with the assembly time budget, and the honest move at that point is
to drop *per-unit* video and keep *per-batch* video plus a per-unit photo. Say that
publicly when it happens; don't quietly degrade a promise.

**Phasing — important.** Phase 1 ships the cheapest version that captures most of
the emotional value: a **numbered unit, a hand-signed card, and 3-4 real photos of
that specific unit mid-build.** Near-zero marginal cost, no video pipeline. Add
video in Phase 2 once units are actually selling. Configuration at checkout should
start as 3 case colors and nothing else — every extra option multiplies filament
inventory, print scheduling, and the chance of shipping the wrong thing.

---

## 5. Operations

**Sourcing — the sharpest operational risk.** The Adafruit NeoKey 1x4 QT is a
single-vendor part (the locked parts list in `CODEX_MICRO_CONSOLE_DESIGN.md` shows
3 units already ordered through Electromaker, i.e. even the reseller path is thin).
Adafruit discontinues and backorders parts routinely, and a 12Key needs **three** of
them, so the flagship is 3x exposed. Mitigations, in order:
1. Buy a buffer early — 30-40 boards (~$400-550) covers roughly 10 mixed-tier units
   and is the cheapest insurance available.
2. Treat the V2 custom PCB as a *supply-chain* project, not a cost project. It is
   already scoped and it removes the dependency entirely.
3. Check the NeoKey's open-hardware license before deriving a PCB from it —
   Adafruit's designs are typically CC-BY-SA, which carries attribution and
   share-alike obligations on derivative board designs.

ESP32-S3 modules, MX switches, keycaps, and ST7789 panels are all multi-source and
low risk. The 2.0" panel is already a known scheduling constraint — the design doc
notes the panel in hand tops out at 140×280 and a proper one is expected ~October,
which gates the 12Key tier specifically.

**3D printing.** One consumer FDM printer, ~4 h per 4Key case and ~7 h per 12Key
case, realistically 4-6 cases per plate for small cases. One printer running evenings
and weekends supports roughly 20-30 cases/month with acceptable failure rates —
comfortably above the 3-6 units/month target, and the reason a print farm is a
Phase 4 question, not a Phase 2 one. Budget 8-12% print failure and price it in
(already in the BOM above).

**Assembly time budget, per unit (estimated, to be replaced with a real time log in
Phase 1):** post-processing and heat-set inserts 15 min · NeoKey header soldering
20-30 min (the firmware README is explicit that friction-fit reads as "no I2C device
found", so this is mandatory and must be done well) · switches, caps, cabling 15 min
· flash and functional test 15 min · packaging and paperwork 10 min. **≈ 1.5 h
hands-on for a 4Key, ~2.5 h for a 12Key**, plus unattended print time. **This is the
hard ceiling on the whole business** (see §7).

**Software maintenance — the recurring, unbounded cost.** The bridge's entire value
depends on two third parties' hook interfaces: `~/.claude/settings.json` hook schema
and `$CODEX_HOME/hooks.json`, plus the event names in `CLAUDE_HOOK_STATUS` /
`CODEX_HOOK_STATUS`. These are documented features, not private APIs, so they won't
change weekly — but they will change, and when they do, every shipped unit goes dark
simultaneously. Design for that now:
- The hook command is already `curl -s --max-time 1 ... || true`, so it fails safe
  and never blocks a customer's CLI. Excellent; keep that property absolutely.
- Add a **version/health check**: the bridge should detect "hooks installed but no
  events received in N sessions" and tell the user, rather than presenting dead keys.
- Budget **2-4 hours a month** of unpaid maintenance forever, and re-run
  `install-hooks` verification after each CLI release.
- macOS, Claude Code, and Codex releases each independently break things. The
  accessibility-permission dance for push-to-talk (documented in
  `host/README_BRIDGE.md`: grant permission, then restart the bridge) is already a
  guaranteed support ticket generator.

**Support burden.** Assume **30-60 min per unit in year one**, front-loaded on:
Accessibility permissions, hooks only applying to sessions started *after*
`install-hooks` (a known limitation, and a confusing one), and the LaunchAgent.
Defenses: a 3-minute setup video, a real troubleshooting page, a
`switchboard_bridge.py doctor` command that self-diagnoses (port, hooks installed,
hook server reachable, last event timestamp), and the plug-and-play CDC+MSC installer
already spiked in `docs/design/plug-and-play-installer-plan.md` — which is a *support
cost reduction* project and should be prioritized as one.

**One honest liability note.** The bridge posts synthetic keystrokes into a live,
metered AI session, and `host/README_BRIDGE.md` records that an earlier version once
landed a `/voice` keystroke as a real chat message — "a real, billed turn." Selling
this to strangers means someone will eventually blame an unexpected API bill on the
device. Required before first paid sale: prominent disclaimer, no automation of
mutating actions by default, an obvious kill switch, and terms disclaiming liability
for third-party API costs.

---

## 6. Go-to-market

**One channel to start: a demo video posted to the Claude Code / AI-coding community
(r/ClaudeAI or r/ClaudeCode plus X/Twitter), pointing to a one-page waitlist — not a
store.**

Why this one:
- **The product is video-native and nothing else.** In text this is "a keypad with
  LEDs." In 20 seconds of video — three keys breathing in different colors, one
  flipping to green while you're looking elsewhere, the hue creeping toward ember as
  a turn drags on — it is instantly legible. The duration-based ramp built in commit
  `fced269` is the single most demo-able thing in the repo. Lead with it.
- **The Tier 1 customer is concentrated there,** already complaining about exactly
  this problem in exactly those threads. No targeting work required.
- **A waitlist, not a store, is the correct first artifact** because there is no
  repeatable build process yet. A waitlist converts interest into a queue you can
  serve at your own pace, and — critically — it *measures demand for $0* before any
  inventory is bought.

Why not the alternatives, specifically:
- **Show HN:** worth doing, but it is a one-shot card and it rewards open source with
  stars, not hardware with orders. Spend it in Phase 2 when there is something to
  buy, and expect it to move GitHub traffic more than units.
- **r/MechanicalKeyboards / r/olkb:** a build-log post there will get sharp,
  useful critique of the case and switches, and approximately zero people who
  understand why they'd want it. Use it for feedback in Phase 1, not for sales.
- **Kickstarter — actively wrong for this project.** Crowdfunding converts a
  flexible spare-time hobby into a fixed legal delivery obligation, on a
  single-sourced part, with a build process that has never been repeated once, for a
  product whose core software depends on a third party that could break it mid-campaign.
  Every structural feature of this business argues against pre-selling. **Made-to-order
  with an honest 2-4 week lead time and a deposit** achieves the same cash-flow goal
  with none of the exposure.

Supporting, near-zero cost: keep the GitHub repo public and good — it is the
credibility layer, and the free-software crowd who build their own were never
customers. Gift 2-3 units to AI-dev YouTubers in Phase 2.

---

## 7. Risks

Ranked by expected damage, not by likelihood.

**1. Platform dependency (highest).** The product's entire function is a live readout
of someone else's tool, delivered through Claude Code and Codex CLI lifecycle hooks.
If those event names change, if the CLIs ship their own native multi-session
dashboard, or if the ecosystem drifts from CLI to GUI, every unit already on a
customer's desk becomes a keypad with pretty lights — simultaneously, with no notice,
with zero recourse, and with a solo operator who cannot ship a fix and answer 30
support emails in the same weekend. *Mitigations:* fail-safe hooks (already in
place), self-diagnosing `doctor` command, a documented "works as a plain macropad"
fallback mode so the object retains standalone value, published support window,
conservative refund policy, and deliberately keeping the firmware protocol generic
(it already is — `agent.update` carries a status string, not a vendor concept) so a
third CLI family is an afternoon of work.

**2. IP and trademark — address this before the first sale, not after.** The device
visualizes third-party tools, and the naming history in this repo has already drifted
across other companies' marks: "Codex Micro Console" (OpenAI's product name), the
repo path `Codex_Micro_Display`, and `README_CODEX_MICRO.md`. `CODEX_MICRO_CONSOLE_DESIGN.md`
already made the right call for the right reason — renamed to **Switchboard** because
Codex-only branding misrepresented an agent-agnostic device. Finish that job:
- Ship under a brand that contains **no** third-party mark. Never "Claude Key,"
  "CodexPad," or similar, and no `claude*`/`codex*` domain.
- Clear "Switchboard" properly before printing it on anything — it is a common
  English word and almost certainly crowded in the relevant trademark classes. Search
  USPTO plus the EU/UK registers; be prepared to pick something more distinctive.
  "4Key/8Key/12Key" are descriptive and therefore weak as marks but safe to use.
- Marketing may describe compatibility — "works with Claude Code and Codex CLI" is
  ordinary nominative use. It may **not** use their logos, their type treatments,
  their color identities, or any phrasing implying partnership or endorsement. Put a
  plain disclaimer on the product page: not affiliated with or endorsed by Anthropic
  or OpenAI.
- Rename the repo, the docs, and the firmware sketch filename in Phase 0. Consistency
  is cheap now and expensive after 50 units ship in printed boxes.
- Separately: check the Adafruit NeoKey's open-hardware license before deriving the
  V2 PCB from it, and don't market around "NeoKey" or "Adafruit" as if they were your
  brands.

**3. Single point of failure: the person.** Illness, a busy quarter at the day job, or
simple boredom stops fulfillment, support, and maintenance at the same instant. There
is no backup and no employee, and this is unfixable at this scale — so manage it
structurally instead: never hold a backlog longer than 3-4 weeks, state lead times
honestly and pad them, take deposits rather than full prepayment, keep a documented
build procedure so *you in three months* can still assemble a unit, and put a written
"if I go quiet" policy (refund terms, open-sourced firmware and case STLs) on the
product page. That last item is also a genuine trust-builder for a one-person
hardware brand.

**4. Component sourcing.** See §5. Single US vendor, 3 boards per flagship unit.
Buffer stock and the V2 PCB are the answers.

**5. Not a repeatable manufacturing process.** One unit has been built. The firmware
README already documents that this board needed a hand-rolled `begin()` because a
seesaw software reset bricked its I2C reads — evidence that per-unit bring-up
debugging is a real cost, not a hypothetical. Until 3 consecutive units are built
from a written procedure with no surprises, **there is no product**, only a prototype
that happens to work. Phase 1 exists entirely to retire this risk.

**6. Free substitutes and near-zero switching cost.** OpenMicro does the software
part for free with a game controller; this repo's own software is public. Anyone
technical enough to run three Claude Code sessions can wire a $20 macropad in an
evening. *This is survivable and does not invalidate the business* — people who
would do that were never buyers — but it does dictate strategy: **the defensible
part is the object and the experience, not the status detection.** Which is precisely
why §4 matters more than it first appears.

**7. Marketing a feature that doesn't work.** `blocked`/red is unreachable today,
and mutating actions are all disabled. Shipping marketing ahead of the code produces
refunds and a bad first review, which for a one-person brand is close to fatal.
Market only what `switchboard_bridge.py` actually sets today.

---

## 8. Staged ramp

Sized for **4-8 hours a week**. Each phase has one exit test; do not start the next
phase before passing it.

### Phase 0 — Finish the prototype and measure demand
**~6-8 weeks · 4-6 h/week · $0-200 (parts already ordered)**

- Generalize `AGENT_KEY_COUNT`/`PTT_KEY_INDEX` to a configurable key count so one
  firmware serves 4/8/12 (currently hard-coded at 3+1).
- Bring up the 12-key configuration with the three ordered NeoKey boards.
- Land a real `blocked` signal, or formally drop red from the marketing story.
- Add `switchboard_bridge.py doctor`.
- Finish the naming/trademark work in §7.2 and rename the repo and docs.
- **Shoot the demo video and publish the waitlist page.** Cost: one evening.

**Exit test: 50+ genuine waitlist signups.** Under ~20, the honest read is that this
is a great personal tool and the business stops here — and that is a perfectly good
outcome, having cost $200.

### Phase 1 — Make it repeatable
**~2-3 months · 5-8 h/week · $400-700**

- Design the enclosure (start with 4Key only — one tier, one color).
- Print and build **three** identical units from a written procedure, logging actual
  minutes per step. Revise the procedure until unit #3 has no surprises.
- Buy NeoKey buffer stock (30-40 boards).
- Ship 2-3 units at cost to waitlist volunteers in exchange for blunt feedback and
  photos. Watch them do setup without help; every stumble is a support ticket you're
  pre-paying to discover.
- Write terms, refund policy, and the liability disclaimer from §5.

**Exit test: three units built from the procedure with no rework, real per-unit time
under 2 h, and at least two testers set up unaided.**

### Phase 2 — First 10 paid units
**~3-4 months · 6-10 h/week · $600-1,200 inventory**

- Open made-to-order sales: 4Key $149 and 8Key $229 (hold 12Key until the 2.0" panel
  is in hand and proven). 3 case colors. Honest 3-week lead time. Deposit, not full
  prepayment.
- Ship the cheap build-experience version: numbered unit, signed card, 3-4 real
  photos of that unit mid-build.
- Now spend the Show HN card.
- Gift 2 units to creators.

**Revenue: ~$1,500-2,300 gross, ~$1,000-1,600 gross profit.**
**Exit test: average support time under 1 h/unit, zero RMAs for assembly faults, and
at least three unsolicited public posts from customers.** If support runs to 3 h/unit,
fix onboarding (the CDC+MSC installer) before taking another order.

### Phase 3 — Sustainable spare-time income
**Ongoing · 8-12 h/week · self-funding**

- Steady state **3-6 units/month**: ~$600-1,500 revenue, **~$400-1,000/month gross
  profit** before your own time.
- Add the 12Key once the panel lands; add per-unit build video once the queue is
  predictable.
- Start the V2 custom PCB — justified now on sourcing risk *and* ~$15/unit margin.
- Maintain: 2-4 h/month on CLI hook drift, forever.

**Honest ceiling.** At ~1.5-2.5 h assembly plus ~0.5-1 h support per unit, a 10 h/week
operator saturates around **20-25 units/month**, or roughly **$1,500-2,500/month gross
profit** — and at that point it is a second job, not a side project. Breaking that
ceiling requires the V2 PCB, batched print runs, outsourced assembly, or higher prices.
Phase 4 is a decision, not a plan: cap it deliberately, or stop calling it spare time.

---

## 9. Recommendation

**Build the product ladder first. Ship the build-experience as photos, not video, and
not live — ever.**

The "watch your unit get made" idea is genuinely good, and it is correctly aimed: for
a hand-built object sold by one person, provenance *is* the product, and the artisan
keyboard and knife communities prove people pay for it. But almost all of its
emotional value is captured by the cheapest possible implementation — a numbered unit,
a signed card, and four real photographs of *your* case coming off the plate — which
costs about ten minutes and no new equipment. The expensive implementations buy
surprisingly little on top: a batch timelapse plus a 60-second per-unit assembly clip
adds maybe $20-40 of willingness-to-pay, and **live per-order streaming should be
struck from the plan entirely.** It turns every sale into a scheduled performance,
consumes the exact hours that assembly needs, broadcasts a private home, and risks
debugging I2C in front of a paying customer — and nobody was going to watch a
four-hour print anyway.

More importantly, the build experience is a *conversion multiplier*, not a demand
generator. It makes someone who already wants the object want it more. It cannot make
someone want a $229 agent-status keypad in the first place — that job belongs to the
20-second video of three keys breathing blue-to-ember while an agent grinds through a
long turn, which is the strongest asset this project already has and which costs one
evening to produce. Spending Phase 1 on a camera pipeline instead of on an enclosure
and a repeatable build procedure would be optimizing the wrong half of a business that
has not yet sold a single unit.

So: Phase 0 measures whether anyone wants this at all, for about $200 and a few
evenings. Phase 1 proves a second and third unit can be built without surprises —
which, given that the one existing unit needed a hand-rolled `begin()` to survive its
own I2C chip, is not a formality. Only then does the build-experience layer get
anything richer than photographs.

**The biggest risk is not manufacturing, sourcing, or demand — it is platform
dependency.** The entire value proposition is a live readout of another company's
tool, plumbed through Claude Code and Codex CLI lifecycle hooks. When those change, or
when either vendor ships a native multi-session view, every unit already on a desk
goes dark at once, and one person in their spare time cannot patch the fleet and
answer the support queue in the same weekend. The hooks failing safe (`|| true`) is a
strong start. Finish the job: add a self-diagnosing `doctor` command, keep the wire
protocol vendor-neutral so a third CLI is an afternoon's work, ship a documented
plain-macropad fallback so the object keeps standalone value, and state the support
window and refund policy in writing before the first sale. Do that, and the worst case
is a disappointing month rather than a dead product and a folder full of refund
requests.
