(function () {
  "use strict";

  var TOKEN = window.SWITCHBOARD_TOKEN || "";
  var SLOTS = [1, 2, 3]; // key 4 is push-to-talk, not an agent slot
  var EFFORT_VALUES = ["low", "medium", "high", "xhigh", "max"];

  // Mirrors the LED colors the board actually shows for each status
  // (see status_table.py) so the UI's status pill and the board agree
  // at a glance. Busy/unknown have no single fixed LED color (busy uses
  // a ramp; unknown pulses) — "busy"/"unknown" here are just the closest
  // static approximation.
  var STATUS_PILL_CLASS = {
    empty: "pill-muted",
    launched: "pill-muted",
    idle: "pill-muted",
    thinking: "pill-busy",
    working: "pill-busy",
    waiting: "pill-warn",
    needs_input: "pill-warn",
    blocked: "pill-err",
    done: "pill-ok",
    unknown: "pill-unknown",
  };

  var TIER_PILL_CLASS = { full: "pill-ok", status: "pill-busy", launch_only: "pill-muted" };
  var TIER_LABEL = { full: "Full", status: "Status", launch_only: "Launch-only" };

  // Mirrors switchboard.settings.LED_IDLE_BREATHE_PRESETS /
  // LED_THINKING_CYCLE_PRESETS — the board only ever sees the resolved
  // millisecond values (led_config_wire), never these names, so this
  // list is safe to extend on either side independently.
  var LED_IDLE_BREATHE_OPTIONS = [
    { value: "off", label: "Off (solid)" },
    { value: "medium", label: "Medium (4s)" },
    { value: "slow", label: "Slow (7s) — giant breath" },
  ];
  var LED_IDLE_BREATHE_MS = { off: 0, medium: 4000, slow: 7000 };
  var LED_THINKING_CYCLE_OPTIONS = [
    { value: "quick", label: "Quick (90s)" },
    { value: "normal", label: "Normal (5 min)" },
    { value: "slow", label: "Slow (10 min)" },
  ];
  var LED_THINKING_CYCLE_MS = { quick: 90000, normal: 300000, slow: 600000 };

  var state = {
    document: null,
    versionToken: null,
    families: [],
    voiceProviders: [],
    status: {},
  };

  function $(sel, root) { return (root || document).querySelector(sel); }

  function banner(message) {
    var el = $("#banner");
    if (!message) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = message;
  }

  function setBridgeStatus(ok, text) {
    var el = $("#bridge-status");
    el.innerHTML = "";
    var dot = document.createElement("span");
    dot.className = "pill-dot";
    el.appendChild(dot);
    el.appendChild(document.createTextNode(text));
    el.className = "pill " + (ok ? "pill-ok" : "pill-err");
  }

  function fetchJSON(url, options) {
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { ok: res.ok, status: res.status, body: body };
      });
    });
  }

  function fillSelect(select, options, selected) {
    select.innerHTML = "";
    options.forEach(function (opt) {
      var el = document.createElement("option");
      el.value = opt.value;
      el.textContent = opt.label;
      if (opt.value === selected) el.selected = true;
      select.appendChild(el);
    });
  }

  function slotDoc(number) {
    var slots = (state.document && state.document.slots) || [];
    for (var i = 0; i < slots.length; i++) {
      if (slots[i].slot === number) return slots[i];
    }
    return { slot: number };
  }

  function statusFor(number) {
    var slots = (state.status && state.status.slots) || [];
    for (var i = 0; i < slots.length; i++) {
      if (slots[i].slot === number) return slots[i];
    }
    return null;
  }

  function familyChoices() {
    var choices = state.families.map(function (f) {
      return { value: f.name, label: f.display_name + (f.detected ? " ✓" : " (not found)") };
    });
    choices.push({ value: "__custom__", label: "Custom command…" });
    return choices;
  }

  function renderSlotCard(number) {
    var template = $("#slot-card-template");
    var node = template.content.firstElementChild.cloneNode(true);
    var doc = slotDoc(number);
    var live = statusFor(number);

    $(".keycap", node).textContent = String(number);
    $(".f-name", node).value = doc.name || "";
    $(".f-name", node).placeholder = "Agent " + number;

    var familySelect = $(".f-family", node);
    var badge = $(".f-badge", node);
    var knownNames = state.families.map(function (f) { return f.name; });
    var currentFamily = doc.family;
    var isCustom = currentFamily && knownNames.indexOf(currentFamily) === -1;
    fillSelect(familySelect, familyChoices(), isCustom ? "__custom__" : (currentFamily || ""));

    var commandRow = $(".f-command-row", node);
    var commandInput = $(".f-command", node);
    commandInput.value = doc.command || "";

    function syncFamilyBadge() {
      var fam = state.families.filter(function (f) { return f.name === familySelect.value; })[0];
      if (!fam) {
        badge.textContent = "";
        badge.className = "f-badge field-note";
        return;
      }
      badge.textContent = fam.detected ? "✓ found at " + fam.path : "not found on this machine";
      badge.className = "f-badge field-note " + (fam.detected ? "ok" : "warn");
    }

    function syncCommandVisibility() {
      var isCustomNow = familySelect.value === "__custom__";
      commandRow.hidden = !isCustomNow;
      if (!isCustomNow) {
        // Known families launch their own binary by convention (family
        // name == command name, e.g. "claude" -> claude, "codex" -> codex).
        commandInput.value = familySelect.value;
      }
      syncFamilyBadge();
    }
    familySelect.addEventListener("change", syncCommandVisibility);
    syncCommandVisibility();
    if (isCustom) commandRow.hidden = false; // keep the real custom command visible on load

    $(".f-cwd", node).value = doc.cwd || "";

    var effortSelect = $(".f-effort", node);
    fillSelect(
      effortSelect,
      EFFORT_VALUES.map(function (v) { return { value: v, label: v }; }),
      doc.effort || (state.document.defaults && state.document.defaults.effort) || "medium"
    );

    var voiceProviderSelect = $(".f-voice-provider", node);
    var voice = doc.voice || {};
    fillSelect(
      voiceProviderSelect,
      state.voiceProviders.map(function (p) {
        return { value: p.name, label: p.display_name + (p.available ? "" : " (not available yet)") };
      }),
      voice.provider || "none"
    );

    var voiceFields = $(".voice-fields", node);
    var chordInput = $(".f-voice-chord", node);
    var modeSelect = $(".f-voice-mode", node);
    chordInput.value = voice.chord || "";
    fillSelect(modeSelect, [{ value: "hold", label: "Hold" }, { value: "toggle", label: "Toggle" }], voice.mode || "hold");

    function syncVoiceVisibility() {
      voiceFields.hidden = voiceProviderSelect.value === "none";
    }
    voiceProviderSelect.addEventListener("change", syncVoiceVisibility);
    syncVoiceVisibility();

    var statusWord = live ? live.status : "empty";
    var statusPill = $(".f-status", node);
    statusPill.textContent = statusWord;
    statusPill.className = "f-status pill " + (STATUS_PILL_CLASS[statusWord] || "pill-muted");

    var family = state.families.filter(function (f) { return f.name === (currentFamily || ""); })[0];
    var tierPill = $(".f-tier", node);
    if (family) {
      tierPill.textContent = TIER_LABEL[family.tier] || family.tier;
      tierPill.className = "f-tier pill " + (TIER_PILL_CLASS[family.tier] || "pill-muted");
      tierPill.hidden = false;
    } else {
      tierPill.hidden = true;
    }

    node.dataset.slot = String(number);
    return node;
  }

  function updateLedPreview() {
    var idleValue = $("#led-idle-breathe").value;
    var cycleValue = $("#led-thinking-cycle").value;
    var idleDot = $("#led-preview-idle");
    var idleMs = LED_IDLE_BREATHE_MS[idleValue] || 0;
    idleDot.classList.toggle("solid", idleMs === 0);
    idleDot.style.setProperty("--led-idle-ms", idleMs + "ms");
    $("#led-preview-busy").style.setProperty("--led-busy-ms", (LED_THINKING_CYCLE_MS[cycleValue] || 300000) + "ms");
  }

  function render() {
    var container = $("#slots");
    container.innerHTML = "";
    SLOTS.forEach(function (n) { container.appendChild(renderSlotCard(n)); });

    fillSelect($("#default-effort"), EFFORT_VALUES.map(function (v) { return { value: v, label: v }; }), (state.document.defaults && state.document.defaults.effort) || "medium");
    $("#default-cwd").value = (state.document.defaults && state.document.defaults.cwd) || "";

    var leds = state.document.leds || {};
    fillSelect($("#led-idle-breathe"), LED_IDLE_BREATHE_OPTIONS, leds.idle_breathe || "off");
    fillSelect($("#led-thinking-cycle"), LED_THINKING_CYCLE_OPTIONS, leds.thinking_cycle || "normal");
    updateLedPreview();
  }

  function collectDocument() {
    var slots = SLOTS.map(function (number) {
      var card = document.querySelector('.slot-card[data-slot="' + number + '"]');
      var name = $(".f-name", card).value.trim();
      var family = $(".f-family", card).value;
      var command = $(".f-command", card).value.trim();
      var cwd = $(".f-cwd", card).value.trim();
      var effort = $(".f-effort", card).value;
      var voiceProvider = $(".f-voice-provider", card).value;

      var slot = { slot: number };
      if (name) slot.name = name;
      if (family && family !== "__custom__") slot.family = family;
      if (command) slot.command = command;
      if (cwd) slot.cwd = cwd;
      if (effort) slot.effort = effort;
      if (voiceProvider && voiceProvider !== "none") {
        var voice = { provider: voiceProvider };
        var chord = $(".f-voice-chord", card).value.trim();
        if (chord) voice.chord = chord;
        voice.mode = $(".f-voice-mode", card).value;
        slot.voice = voice;
      }
      return slot;
    });

    var defaults = {};
    var defaultCwd = $("#default-cwd").value.trim();
    var defaultEffort = $("#default-effort").value;
    if (defaultCwd) defaults.cwd = defaultCwd;
    if (defaultEffort) defaults.effort = defaultEffort;

    var leds = {};
    var idleBreathe = $("#led-idle-breathe").value;
    var thinkingCycle = $("#led-thinking-cycle").value;
    if (idleBreathe) leds.idle_breathe = idleBreathe;
    if (thinkingCycle) leds.thinking_cycle = thinkingCycle;

    return { settings_version: 2, defaults: defaults, slots: slots, leds: leds };
  }

  function save() {
    var saveStatus = $("#save-status");
    var button = $("#save-btn");
    button.disabled = true;
    saveStatus.textContent = "Saving…";
    saveStatus.className = "save-status";

    fetchJSON("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Switchboard-Token": TOKEN },
      body: JSON.stringify({ document: collectDocument(), version_token: state.versionToken }),
    }).then(function (result) {
      button.disabled = false;
      if (result.status === 200) {
        state.document = result.body.document;
        state.versionToken = result.body.version_token;
        saveStatus.textContent = "Saved. Takes effect on each slot's next launch.";
        saveStatus.className = "save-status ok";
        render();
        return;
      }
      if (result.status === 409) {
        saveStatus.textContent = "Settings changed elsewhere — reloaded the latest version. Review and save again.";
        saveStatus.className = "save-status err";
        state.document = result.body.current.document;
        state.versionToken = result.body.current.version_token;
        render();
        return;
      }
      if (result.status === 422) {
        var messages = (result.body.errors || []).map(function (e) { return e.path + ": " + e.message; });
        saveStatus.textContent = messages.join("; ") || "Could not save.";
        saveStatus.className = "save-status err";
        return;
      }
      saveStatus.textContent = "Could not save (" + result.status + ").";
      saveStatus.className = "save-status err";
    }).catch(function () {
      button.disabled = false;
      saveStatus.textContent = "Could not reach the bridge.";
      saveStatus.className = "save-status err";
      setBridgeStatus(false, "bridge: unreachable");
    });
  }

  function loadAll() {
    return Promise.all([
      fetchJSON("/api/settings"),
      fetchJSON("/api/families"),
      fetchJSON("/api/voice-providers"),
      fetchJSON("/api/status"),
    ]).then(function (results) {
      var settingsRes = results[0], familiesRes = results[1], voiceRes = results[2], statusRes = results[3];
      if (!settingsRes.ok) throw new Error("settings fetch failed");
      state.document = settingsRes.body.document;
      state.versionToken = settingsRes.body.version_token;
      state.families = familiesRes.body.families || [];
      state.voiceProviders = voiceRes.body.providers || [];
      state.status = statusRes.body || {};
      setBridgeStatus(true, "bridge: running");
      banner(null);
      render();
    }).catch(function () {
      setBridgeStatus(false, "bridge: unreachable");
      banner("Could not reach the bridge. Start it (or reload this page) and try again.");
      $("#save-btn").disabled = true;
    });
  }

  $("#save-btn").addEventListener("click", save);
  $("#led-idle-breathe").addEventListener("change", updateLedPreview);
  $("#led-thinking-cycle").addEventListener("change", updateLedPreview);
  loadAll();
})();
