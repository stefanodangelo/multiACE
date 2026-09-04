const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;
// Behind nginx the UI lives at /multiace; the dev server (scripts/run-dev-ui.*)
// serves it from the root. Deriving the base from the path keeps ONE bundle
// working in both, instead of a build-time constant nobody remembers to flip.
const BASE = location.pathname.startsWith("/multiace") ? "/multiace" : "";
const API = BASE + "/api";
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
             + location.host + BASE + "/ws";
const SCREEN = "/screen";
createApp({
  setup() {
    const _validTabs = new Set(["dashboard", "monitor", "history", "spools", "config"]);
    const _storedTab = localStorage.getItem("multiace.tab");
    const _isPluginTab = (s) => typeof s === "string" && s.startsWith("plugin:");
    const tab = ref(
      (_validTabs.has(_storedTab) || _isPluginTab(_storedTab))
        ? _storedTab
        : "dashboard"
    );
    // Opening the spools tab pays any Spoolman debt left over from an
    // unreachable instance (NAS off overnight: the print-end sync failed,
    // the debt survives on disk but nothing retried it). Backend-side the
    // call is idle-only, rate-limited and push-only - during a print the
    // 3 min tick owns the window, so this stays silent then. Fire and
    // forget: the numbers refresh with the next state poll.
    function spoolmanIdlePush() {
      fetch(`${API}/spoolman/push`, {method: "POST"}).catch(() => {});
    }
    watch(tab, (v) => {
      localStorage.setItem("multiace.tab", v);
      if (v === "spools") spoolmanIdlePush();
    });
    const plugins = reactive({items: [], loaded: false});
    async function refreshPlugins() {
      try {
        const r = await fetch(`${API}/integrations`);
        if (!r.ok) return;
        const j = await r.json();
        plugins.items = j.plugins || [];
      } catch (_) {
      } finally {
        plugins.loaded = true;
        if (_isPluginTab(tab.value)) {
          const pname = tab.value.slice("plugin:".length);
          if (!plugins.items.find(p => p.name === pname)) {
            tab.value = "dashboard";
          }
        }
      }
    }
    function pluginIframeSrc(p) {
      const u = (p && p.ui_url) || "/";
      return `/plugin/${p.name}${u.startsWith("/") ? u : "/" + u}`;
    }
    const language = ref(localStorage.getItem("multiace.lang") || "en");
    const languages = ref([{code: "en", name: "English"}]);
    const catalog = reactive({});
    const indexBase = ref(0);
    function t(key, params) {
      const parts = key.split('.');
      let v = catalog;
      for (const p of parts) {
        if (v == null) return key;
        v = v[p];
      }
      if (typeof v !== "string") return key;
      if (!params) return v;
      return v.replace(/\{(\w+)\}/g, (_, k) => params[k] != null ? params[k] : `{${k}}`);
    }
    function dispIdx(n) {
      if (n == null) return "–";
      return Number(n) + indexBase.value;
    }
    // Subtype label for display: hide the implicit defaults (empty / Basic /
    // generic) so only a meaningful subtype (Matte, Silk, HF, ...) shows.
    function subText(sku) {
      const s = (sku || "").trim();
      // 'none' is stock print_task_config's placeholder (S28) - the backend
      // filters it at the source now, this is the display-edge belt for any
      // path (old backend, hand-typed value) that still carries it.
      if (!s || ["basic", "generic", "none"].includes(s.toLowerCase())) return "";
      return s;
    }
    // Provenance badge label for an identity source (spec §4 / D3):
    // rfid = read from tag, override = user-set, derived = from print job.
    // Empty/raw slots have no badge.
    function sourceLabel(src) {
      if (src === "rfid") return t("ui.common.source_rfid");
      if (src === "override") return t("ui.common.source_override");
      if (src === "derived") return t("ui.common.source_derived");
      return "";
    }
    async function loadCatalog(lang) {
      try {
        const r = await fetch(`${API}/i18n/${lang}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        for (const k of Object.keys(catalog)) delete catalog[k];
        Object.assign(catalog, data);
        document.documentElement.lang = lang;
        if (conn.value.state === "init" || conn.value.state === "warn") {
          conn.value = {
            state: conn.value.state,
            text: conn.value.state === "ok"   ? t("ui.header.live")
                : conn.value.state === "warn" ? t("ui.header.offline")
                : conn.value.state === "err"  ? t("ui.header.ws_error")
                :                               t("ui.header.connecting"),
          };
        }
      } catch (e) {
        console.warn("i18n load failed", e);
      }
    }
    async function loadLanguageList() {
      try {
        const r = await fetch(`${API}/i18n`);
        if (!r.ok) return;
        const j = await r.json();
        if (Array.isArray(j.languages) && j.languages.length) {
          languages.value = j.languages;
        }
      } catch (_) {}
    }
    async function setLanguage(lang) {
      language.value = lang;
      localStorage.setItem("multiace.lang", lang);
      await loadCatalog(lang);
      // Drive the Klipper-side _t() catalog too (pause/error messages) and
      // persist as ace__language, so display popup + Fluidd follow the UI
      // language. Live reload - no Klipper restart needed.
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "MULTIACE_SET_LANGUAGE", args: {LANG: lang}}),
        });
      } catch (_) {}
    }
    // Native <option>s can't carry a flag image next to the label (same
    // limitation as the head-target picker's colour chip - see hm-dd
    // below), so the rail's language picker is a small custom dropdown
    // instead of a <select>. Hand-drawn inline SVGs, not the Unicode flag
    // emoji: regional-indicator flags render as bare letter pairs without
    // a colour-emoji font (stock Windows Chromium, most embedded Linux
    // display images), which defeats "flag icon" entirely on exactly the
    // touchscreen this app also targets. Square (not the flags' native
    // 3:2) so the circular crop below keeps each design's centre - for
    // zh that means the canton is drawn INSIDE the square, not cropped
    // out of a wider rectangle. Unknown codes fall back to text initials
    // so a new catalog file works before anyone draws its flag here.
    const LANG_FLAG_SVG = {
      en: '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        + '<rect width="20" height="20" fill="#012169"/>'
        + '<path d="M0 0 L20 20 M20 0 L0 20" stroke="#fff" stroke-width="4"/>'
        + '<path d="M0 0 L20 20 M20 0 L0 20" stroke="#C8102E" stroke-width="1.4"/>'
        + '<rect x="8" width="4" height="20" fill="#fff"/>'
        + '<rect y="8" width="20" height="4" fill="#fff"/>'
        + '<rect x="8.7" width="2.6" height="20" fill="#C8102E"/>'
        + '<rect y="8.7" width="20" height="2.6" fill="#C8102E"/>'
        + '</svg>',
      de: '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        + '<rect width="20" height="6.67" fill="#000"/>'
        + '<rect y="6.67" width="20" height="6.67" fill="#DD0000"/>'
        + '<rect y="13.33" width="20" height="6.67" fill="#FFCE00"/>'
        + '</svg>',
      zh: '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        + '<rect width="20" height="20" fill="#DE2910"/>'
        + '<g fill="#FFDE00">'
        + '<polygon points="6,3 6.9,5.6 9.6,5.6 7.4,7.2 8.3,9.8 6,8.2 3.7,9.8 4.6,7.2 2.4,5.6 5.1,5.6"/>'
        + '<circle cx="10.5" cy="2.6" r=".6"/><circle cx="12.3" cy="4.8" r=".6"/>'
        + '<circle cx="12.1" cy="7.6" r=".6"/><circle cx="10.1" cy="9.2" r=".6"/>'
        + '</g></svg>',
    };
    function langFlagGlyph(code) {
      return (code || "?").slice(0, 2).toUpperCase();
    }
    function langFlagInner(code) {
      return LANG_FLAG_SVG[code] || langFlagGlyph(code);
    }
    function langName(code) {
      const l = languages.value.find(x => x.code === code);
      return l ? l.name : code;
    }
    const langMenuOpen = ref(false);
    function langPick(code) {
      langMenuOpen.value = false;
      if (code !== language.value) setLanguage(code);
    }
    const version = ref("");
    const printerName = ref("");
    const printerFw = ref("");
    const conn = ref({state: "init", text: ""});
    const connClass = computed(() => ({
      ok:   conn.value.state === "ok",
      warn: conn.value.state === "warn",
      err:  conn.value.state === "err",
    }));
    const connText = computed(() => conn.value.text);
    const screenAvailable = ref(false);
    const state = reactive({
      ace_status: null, ace_temp: null,
      printer_state: null,
      active_device: null, device_count: 0,
      mode: "normal",
      ace_head: 3,
      ace_heads: [],
      head_feeder: {},
      head_feeder_combo: {},
      head_ace: {},
      dryer: null,
      swap_in_progress: false,
      aces: [], toolheads: [], wiring: [],
      save_variables: {},
      bg_swap: {available: false, enabled_heads: [], busy: [], version: null},
      pickup_cleaning: false,
      confirm_commands: false,
      remember_filament: true,
      spoolman_url: "",
      spoolman_auto: false,
      spool_mode: "local",
      spoollink: false,
      spoollink_agent: false,
      airprint_detection: false,
      quad_replenish: false,
      quad_first: false,
      spools: {},
      spool_binding: {},
      auto_dry_masters: [],
      tipform: {available: false, mode: null, tables: []},
      // Send-to-multiACE inbox: a slicer-pushed gcode waiting for pickup.
      preflight_inbox: {pending: false, name: null, size: 0, ts: 0},
    });
    const loadError = ref("");
    // Auto-retry of a failed toolhead load: {head, ace, slot, attempt,
    // max_attempts, next_retry_ms, reason} while a retry is pending, null
    // otherwise - so the banner appears and disappears on its own.
    const retryState = ref(null);
    const aceStartup = ref(null);
    const aceRescanBusy = ref(false);
    const notifications = ref([]);
    // True while every visible notification is warn-level (reconnect
    // attempts etc.) - the strip then wears the amber frame instead of
    // red; one real error flips it back to red.
    const notifWarnOnly = computed(() =>
      notifications.value.length > 0 &&
      notifications.value.every(n => n.level === 'warn'));
    const _notifIds = new Set();
    // Backend stamps ts as epoch (printer clock runs UTC) - format in the
    // BROWSER so the user sees local time. HH:MM:SS, fixed width.
    function notifTime(n) {
      if (!n || !n.ts) return '';
      const d = new Date(n.ts * 1000);
      const p = (x) => String(x).padStart(2, '0');
      return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }
    function _addNotif(n) {
      if (!n || n.id == null) return;
      if (_notifIds.has(n.id)) return;
      _notifIds.add(n.id);
      notifications.value.push(n);
      if (notifications.value.length > 20) {
        const dropped = notifications.value.splice(0, notifications.value.length - 20);
        for (const d of dropped) _notifIds.delete(d.id);
      }
    }
    function onGcodeError(m) {
      _addNotif({id: m.id, ts: m.ts, msg: m.msg, raw: m.raw, level: m.level || 'error'});
    }
    async function loadNotifications() {
      try {
        const r = await fetch(`${API}/notifications`);
        if (!r.ok) return;
        const j = await r.json();
        for (const n of (j.notifications || [])) _addNotif(n);
      } catch (_) {}
    }
    async function dismissNotification(id) {
      const idx = notifications.value.findIndex(n => n.id === id);
      if (idx >= 0) {
        notifications.value.splice(idx, 1);
        _notifIds.delete(id);
      }
      try { await fetch(`${API}/notifications/${id}`, {method: "DELETE"}); } catch (_) {}
    }
    async function dismissAllNotifications() {
      const ids = notifications.value.map(n => n.id);
      notifications.value = [];
      for (const id of ids) _notifIds.delete(id);
      try { await fetch(`${API}/notifications`, {method: "DELETE"}); } catch (_) {}
    }
    function applyState(s) {
      if (!s) return;
      // Klippy is down (firmware_restart / reboot in progress, Moonraker up).
      // Show a "please restart printer" hint instead of the raw 503, and keep
      // the last good dashboard visible. Recovers on the next state once
      // Klipper is back. Reuses loadError (no extra UI); cleared below when a
      // real state arrives so the hint never sticks.
      if (s.klippy === 'disconnected') {
        loadError.value = t('ui.common.please_restart');
        // A restart is in progress -> the pending config/mode change is being
        // applied; drop the persistent "restart needed" banner.
        rebootNeeded.value = false;
        return;
      }
      if (loadError.value === t('ui.common.please_restart')) loadError.value = "";
      state.ace_status    = s.ace_status ?? null;
      state.ace_temp      = s.ace_temp ?? null;
      state.printer_state = s.printer_state ?? null;
      state.active_device = s.active_device ?? null;
      state.device_count  = s.device_count ?? 0;
      state.mode          = s.mode || "normal";
      state.pickup_cleaning = !!s.pickup_cleaning;
      state.confirm_commands = !!s.confirm_commands;
      state.remember_filament = s.remember_filament !== false;
      state.spoolman_url = s.spoolman_url || "";
      state.spoolman_auto = !!s.spoolman_auto;
      state.spool_mode = s.spool_mode || "local";
      state.spoollink = !!s.spoollink;
      state.spoollink_agent = !!s.spoollink_agent;
      state.airprint_detection = !!s.airprint_detection;
      state.quad_replenish = !!s.quad_replenish;
      state.quad_first = !!s.quad_first;
      state.spools = s.spools || {};
      state.spool_binding = s.spool_binding || {};
      state.auto_dry_masters = Array.isArray(s.auto_dry_masters)
        ? s.auto_dry_masters : [];
      // What Klipper RUNS (the cfg side comes from /api/tipform). Missing
      // here, state.tipform kept its declared default {available:false} for
      // ever: tipformRestartPending then read the live mode as "stock"
      // against a cfg mode of "custom" and showed "restart Klipper to
      // apply" permanently, right after a restart too (HW 2026-08-02).
      // Same class as auto_dry_masters above - backend sends it, the state
      // object declares it, applyState never copied it.
      state.tipform = (s.tipform && typeof s.tipform === "object")
        ? s.tipform
        : {available: false, mode: null, tables: []};
      state.ace_head      = (typeof s.ace_head === "number") ? s.ace_head : 3;
      state.ace_heads     = Array.isArray(s.ace_heads) ? s.ace_heads : [];
      state.head_feeder   = (s.head_feeder && typeof s.head_feeder === "object") ? s.head_feeder : {};
      state.head_feeder_combo = (s.head_feeder_combo && typeof s.head_feeder_combo === "object") ? s.head_feeder_combo : {};
      state.head_ace      = (s.head_ace && typeof s.head_ace === "object") ? s.head_ace : {};
      state.dryer         = s.dryer ?? null;
      state.swap_in_progress = !!s.swap_in_progress;
      state.aces          = Array.isArray(s.aces) ? s.aces : [];
      state.toolheads     = Array.isArray(s.toolheads) ? s.toolheads : [];
      state.wiring        = Array.isArray(s.wiring) ? s.wiring : [];
      state.save_variables = s.save_variables || {};
      state.bg_swap       = (s.bg_swap && typeof s.bg_swap === "object")
        ? s.bg_swap
        : {available: false, enabled_heads: [], busy: [], version: null};
      state.preflight_inbox = (s.preflight_inbox && typeof s.preflight_inbox === "object")
        ? s.preflight_inbox
        : {pending: false, name: null, size: 0, ts: 0};
      _maybeAutoOpenInbox();
      if (typeof s.display_index_base === "number") {
        indexBase.value = s.display_index_base;
      }
      for (const a of state.aces) {
        // Duration only - the temperature is a printer-side setting now
        // (ace.auto_dry.temp), shared by the manual start and auto-dry.
        if (!dryerCfg[a.idx]) dryerCfg[a.idx] = {duration: 240};
      }
      // Live auto-retry of a failed load. Null whenever nothing is
      // retrying, so the banner disappears by itself when the load
      // succeeds, is cancelled, or ace.py stops writing the state file.
      retryState.value = (s.retry_state && s.retry_state.active)
        ? s.retry_state : null;
      applyPrintControlState(s);
      // §7: fewer ACEs found at startup than configured. Klipper is up and
      // multiACE keeps rescanning - the banner exists so the user knows to
      // switch the unit on, not to demand a FIRMWARE_RESTART.
      const st = s.ace_startup || {};
      aceStartup.value = (st.state === "waiting") ? st : null;
    }
    async function reloadState() {
      try {
        const r = await fetch(`${API}/state`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        loadError.value = j.error || "";
        applyState(j);
      } catch (e) {
        loadError.value = String(e);
      }
    }
    const macroLog = ref("");
    let _macroLogTimer = null;
    function setMacroLog(msg) {
      macroLog.value = msg || "";
      if (_macroLogTimer) { clearTimeout(_macroLogTimer); _macroLogTimer = null; }
      if (msg) {
        _macroLogTimer = setTimeout(() => {
          macroLog.value = "";
          _macroLogTimer = null;
        }, 5000);
      }
    }
    const dryerCfg = reactive({});
    const cmdQueue = ref([]);
    const visibleQueue = computed(() => cmdQueue.value.filter(it => !it.silent));
    const cmdPaused = ref(false);
    let cmdQueueRunning = false;
    function _newId() {
      return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    }
    function _argsKey(args) {
      const a = args || {};
      return Object.keys(a).sort().map(k => `${k}=${a[k]}`).join('|');
    }
    function enqueue(name, args, opts) {
      return new Promise((resolve) => {
        const key = _argsKey(args);
        const dup = cmdQueue.value.find(it =>
          (it.status === 'queued' || it.status === 'running')
          && it.cmd === name
          && _argsKey(it.args) === key);
        if (dup) { resolve(false); return; }
        const it = reactive({
          id: _newId(),
          cmd: name,
          args: args || {},
          status: 'queued',
          error: '',
          silent: !!(opts && opts.silent),
          _resolve: resolve,
        });
        cmdQueue.value.unshift(it);
        // In the panel the dispatch waits one microtask, so a synchronous
        // burst reaches the dispatcher TOGETHER. Every case that matters is
        // such a burst - load-on-an-occupied-head enqueues unload+load in
        // one run, a loadout its whole action list, the picker save+refresh.
        // Called synchronously the first command was already on its way
        // before the second existed, so the batch check below never saw
        // more than one item (measured). Two presses seconds apart still
        // go one by one - by then the first is at the printer anyway.
        if (panelMode) queueMicrotask(_scheduleAdvance);
        else _scheduleAdvance();
      });
    }
    function removeFromQueue(id) {
      const idx = cmdQueue.value.findIndex(i => i.id === id);
      if (idx < 0) return;
      const it = cmdQueue.value[idx];
      if (it.status === 'running') return;
      cmdQueue.value.splice(idx, 1);
      if (it._resolve) it._resolve(false);
      _scheduleAdvance();
    }
    function pauseQueue() { cmdPaused.value = true; }
    function resumeQueue() {
      cmdPaused.value = false;
      _scheduleAdvance();
    }
    function _scheduleAdvance() {
      if (cmdQueueRunning) return;
      // A batch is on its way: its items are still 'queued' until the POST
      // resolves, so without this the next pass would send them a second
      // time, one by one.
      if (sendingAll.value) return;
      if (cmdPaused.value) return;
      if (cmdQueue.value.length === 0) return;
      // Klipper processes gcode serially: a Load/Unload swap holds
      // its slot for 5-15 min. POSTing /api/macro while
      // state.swap_in_progress would just block waiting for the
      // current swap and eventually hit httpx's ReadTimeout. Let
      // queued items wait visible in the queue; a watcher on
      // state.swap_in_progress re-invokes us when Klipper clears.
      if (state.swap_in_progress) return;
      const arr = cmdQueue.value;
      // In the panel the queue is a liability rather than a safety net:
      // Fluidd drops the camera tile's iframe whenever it pauses its
      // streams (a browser tab switch does exactly that), and everything
      // still waiting in browser memory dies with it. A Load on an occupied
      // head would then unload and never load - press Load, end up empty.
      // So as soon as there is more than one command, hand the whole run to
      // the printer as a single script: Klipper owns the sequence from then
      // on and the tile may vanish. One command needs none of this - it is
      // dispatched immediately and already lives at the printer. sendingAll
      // guards against a second batch while the first is still posting.
      if (panelMode && arr.filter(it => it.status === 'queued').length > 1) {
        sendAllToPrinter();
        return;
      }
      let target = null;
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].status === 'queued') { target = arr[i]; break; }
        if (arr[i].status === 'error')  { return; }
      }
      if (!target) {
        _scheduleIdleClear();
        return;
      }
      _runItem(target);
    }
    function _scheduleIdleClear() {
      const stillActive = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (stillActive) return;
      if (cmdPaused.value) cmdPaused.value = false;
    }
    async function _runItem(it) {
      cmdQueueRunning = true;
      it.status = 'running';
      const parts = [it.cmd];
      for (const [k, v] of Object.entries(it.args || {})) {
        parts.push(`${k}=${v}`);
      }
      const script = parts.join(' ');
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: it.cmd, args: it.args || {}}),
        });
        const j = await r.json();
        if (!r.ok || j.detail) {
          it.status = 'error';
          it.error = String(j.detail || `HTTP ${r.status}`);
          it.silent = false;
          cmdPaused.value = true;
        } else {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          it.status = 'done';
        }
      } catch (e) {
        it.status = 'error';
        it.error = String(e);
        it.silent = false;
        cmdPaused.value = true;
      } finally {
        cmdQueueRunning = false;
        if (it._resolve) it._resolve(it.status !== 'error');
      }
      _scheduleAdvance();
    }
    function run(name, args) { return enqueue(name, args); }
    function clearAllErrors() {
      cmdQueue.value = cmdQueue.value.filter(it => it.status !== 'error');
      cmdPaused.value = false;
      if (notifications.value.length) {
        dismissAllNotifications();
      }
      _scheduleAdvance();
    }
    const sendingAll = ref(false);
    async function sendAllToPrinter() {
      // .reverse() is load-bearing: new items are unshifted to the FRONT and
      // the single-command path walks the array from the END, so the array
      // order is newest-first while the queue itself runs oldest-first.
      // Handing the filtered array to the batch as-is sent the script
      // BACKWARDS - a Load on an occupied head became load-then-unload,
      // i.e. it ended up empty. filter() already returns a copy, so this
      // does not touch the queue.
      const items = cmdQueue.value.filter(it => it.status === 'queued').reverse();
      if (!items.length) return;
      const commands = items.map(it => ({name: it.cmd, args: it.args || {}}));
      sendingAll.value = true;
      try {
        const r = await fetch(`${API}/macro-batch`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({commands}),
        });
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        for (const it of items) {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          if (it._resolve) it._resolve(true);
        }
        setMacroLog(t("ui.queue.send_all_done", {count: commands.length}));
      } catch (e) {
        setMacroLog(`${t("ui.queue.send_all_failed")}: ${e.message || e}`);
        confirm({
          title: t("ui.queue.send_all_failed"),
          message: String(e.message || e),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
      } finally {
        sendingAll.value = false;
      }
    }
    function fmtArgs(args) {
      if (!args) return "";
      const parts = [];
      for (const [k, v] of Object.entries(args)) {
        const s = String(v);
        parts.push(`${k}=${s.length > 12 ? s.slice(0, 12) + '…' : s}`);
      }
      return parts.join(' ');
    }
    function cmdLabel(it) {
      const a = it.args || {};
      const di = (n) => dispIdx(Number(n));
      switch (it.cmd) {
        case 'SET_PRINT_FILAMENT_CONFIG':
          return `Display T${di(a.CONFIG_EXTRUDER ?? 0)}`;
        case 'ACE_LOAD_HEAD':
          return `Load T${di(a.HEAD ?? 0)} ← ACE ${di(a.ACE ?? 0)}`;
        case 'ACE_SWAP_HEAD':
          return `Swap T${di(a.HEAD ?? 0)} ← ACE ${di(a.ACE ?? 0)}`;
        case 'ACE_UNLOAD_HEAD':
          return `Unload T${di(a.HEAD ?? 0)}`;
        case 'ACE_UNLOAD_ALL_HEADS':
          return 'Unload all';
        case 'ACE_SWITCH':
          return `ACE ${di(a.TARGET ?? 0)}` + ((a.AUTOLOAD == 1 || a.AUTOLOAD === true) ? ' (auto-load)' : '');
        case 'ACE_DRY':
          return `Dry ACE ${di(a.ACE ?? 0)} ${a.TEMP}°C / ${a.DURATION}min`;
        case 'ACE_STOP_DRYING':
          return `Stop dry ACE ${di(a.ACE ?? 0)}`;
      }
      return null;
    }
    function slotTitle(ace, slot) {
      const bits = [`ACE ${dispIdx(ace.idx)} / Slot ${dispIdx(slot.idx)}`];
      if (slot.material) bits.push(slot.material);
      if (slot.brand) bits.push(slot.brand);
      bits.push(slot.state);
      if (slot.color) bits.push(slot.color);
      return bits.join(" · ");
    }
    const wiringContainerEl = ref(null);
    const slotEls = {};
    const thEls = {};
    const layoutTick = ref(0);
    function setSlotEl(ace, slot, el) {
      const k = `${ace}_${slot}`;
      if (el) slotEls[k] = el; else delete slotEls[k];
    }
    function setThEl(idx, el) {
      if (el) thEls[idx] = el; else delete thEls[idx];
    }
    const wiringPaths = ref([]);
    const wiringViewBox = ref("0 0 100 100");
    function recomputeWiring() {
      // Collapsed, every getBoundingClientRect() below reads zero and the
      // overlay would draw - or worse, cache - garbage. The panel body is
      // v-if so the refs are already gone; this is the belt to that
      // braces, and covers the frame in between.
      if (!secOpen("lanes")) { wiringPaths.value = []; return; }
      const c = wiringContainerEl.value;
      if (!c) { wiringPaths.value = []; return; }
      const cb = c.getBoundingClientRect();
      wiringViewBox.value = `0 0 ${cb.width} ${cb.height}`;
      const lines = [];
      for (const w of state.wiring) {
        const sEl = slotEls[`${w.ace}_${w.slot}`];
        const tEl = thEls[w.toolhead];
        if (!sEl || !tEl) continue;
        const sb = sEl.getBoundingClientRect();
        const tb = tEl.getBoundingClientRect();
        const x1 = sb.left + sb.width / 2 - cb.left;
        const y1 = sb.bottom - cb.top;
        const x2 = tb.left + tb.width / 2 - cb.left;
        const y2 = tb.top - cb.top;
        const midY = (y1 + y2) / 2;
        lines.push({
          d: `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`,
          color: w.color || "#888",
        });
      }
      wiringPaths.value = lines;
    }
    function scheduleWiringRecompute() {
      nextTick(() => {
        recomputeWiring();
        requestAnimationFrame(recomputeWiring);
      });
    }
    // Resume the queue automatically the moment Klipper's swap flag
    // flips back to false. Without this the queue would only advance
    // on the next user action.
    watch(() => state.swap_in_progress, (v) => { if (!v) _scheduleAdvance(); });

    watch(() => state.wiring, scheduleWiringRecompute, {deep: true});
    watch(() => state.aces.length, scheduleWiringRecompute);
    watch(() => state.toolheads.length, scheduleWiringRecompute);
    watch(() => tab.value, (v) => { if (v === "dashboard") scheduleWiringRecompute(); });
    function switchAce(idx) {
      run("ACE_SWITCH", {TARGET: idx});
    }
    // --- Panel mode (?panel=1) -------------------------------------------
    // Compact embeddable view, for Fluidd's "HTTP Page" camera type. A MODE
    // of this app, not a second interface: same state, macros, queue,
    // confirmations (window.confirm works fine inside an iframe) and i18n.
    const _q = new URLSearchParams(location.search);
    const panelMode = _q.get("panel") === "1";
    // The full UI is this same page without ?panel=1. Derived from the
    // current URL rather than hardcoded, so it stays right whether we are
    // served under /multiace/ or off the root in a dev setup. Opened in a
    // new tab on purpose - a plain link would navigate the camera TILE to
    // the full interface, inside a 190px box.
    const fullUiHref = location.pathname;
    // The panel is transparent so the Fluidd camera card paints the
    // background behind it - but opened DIRECTLY there is nothing behind
    // it and transparent falls through to the browser's white. Flag the
    // embedded case instead; the stylesheet keeps the app's own dark
    // background by default, so the standalone view never flashes white.
    if (panelMode) {
      try {
        if (window.self !== window.top) {
          document.body.classList.add("panel-embedded");
        }
      } catch (e) {
        // Cross-origin access to window.top throws - that only happens
        // when we ARE embedded, so treat it as such.
        document.body.classList.add("panel-embedded");
      }
    }
    // Which unit the panel SHOWS. Never ACE_SWITCH - that changes the
    // printer's ACTIVE unit, and looking at another card must not do that.
    // null = follow the active unit until the user picks one.
    const _panelAcePinned = ref((() => {
      const p = parseInt(_q.get("ace"), 10);
      if (!isNaN(p)) return p;                       // ?ace=2 pins it
      const s = localStorage.getItem("multiace.panelAce");
      return s === null ? null : parseInt(s, 10);
    })());
    const panelAceIdx = computed(() => {
      const p = panelPage.value;
      return p && p.kind === 'ace' ? p.ace : null;
    });
    const panelAce = computed(() =>
      (state.aces || []).find(a => a.idx === panelAceIdx.value) || null);
    // Panel PAGES (Dirk 2026-08-09, second cut): the tab strip is the aces
    // plus ONE "Feeder" tab when any feeder/manual head exists - "1, 2,
    // Feeder". The first cut appended the head cards to EVERY ace page;
    // the per-head-tab idea died the same day (one tab per feeder head
    // clutters the strip the moment two exist). Manual heads ride the same
    // page - "analog zu manual (also zukuenftig)" - and when ONLY manual
    // heads exist the tab says Manual instead.
    const panelFeederHeads = computed(() =>
      (state.toolheads || []).filter(t => t.feeder || t.manual || t.combo_feeder));
    const panelPages = computed(() => {
      const pages = (visibleAces.value || []).map(a =>
        ({id: 'a' + a.idx, kind: 'ace', ace: a.idx,
          label: String(dispIdx(a.idx))}));
      if (panelFeederHeads.value.length) {
        const anyFeeder = panelFeederHeads.value.some(x => x.feeder || x.combo_feeder);
        pages.push({id: 'feeders', kind: 'feeders',
                    label: t(anyFeeder ? 'ui.dashboard.feeder'
                                       : 'ui.dashboard.manual')});
      }
      return pages;
    });
    const panelPageId = computed(() => {
      const pages = panelPages.value;
      let pin = _panelAcePinned.value;
      if (pin !== null && /^\d+$/.test(String(pin))) pin = 'a' + pin;
      if (pin !== null && pages.some(p => p.id === pin)) return pin;
      // Default follows the ACTIVE device, like the old ace-only strip.
      if (state.active_device !== null
          && pages.some(p => p.id === 'a' + state.active_device)) {
        return 'a' + state.active_device;
      }
      return pages.length ? pages[0].id : null;
    });
    const panelPage = computed(() =>
      panelPages.value.find(p => p.id === panelPageId.value) || null);
    function setPanelPage(id) {
      _panelAcePinned.value = id;
      // Remembered per browser on purpose: which card you are looking at is
      // a VIEW preference, unlike confirm_commands (a safety setting, which
      // lives on the printer so phone and desktop cannot disagree).
      try { localStorage.setItem("multiace.panelAce", String(id)); } catch (e) {}
    }
    // Unload is per HEAD. Multi wires slot N to head N; in head mode only
    // the ACE head unloads, and only its own slots offer it.
    function panelSlotHead(aceIdx, slotIdx) {
      if (state.mode === "head") {
        const h = state.ace_head;
        return (headAceOf(h) === aceIdx) ? h : null;
      }
      return slotIdx;
    }
    // Which head this slot currently FEEDS, for the card's "T2" corner.
    // head_source is the truth in both modes: multi wires slot N to head N,
    // but that says nothing about whether it is loaded, and head mode maps
    // freely. null = feeds nothing right now, so the corner stays empty.
    function panelSlotHeadLoaded(aceIdx, slotIdx) {
      const th = (state.toolheads || []).find(
        t_ => t_.head_source_known && t_.ace === aceIdx && t_.slot === slotIdx);
      return th ? th.idx : null;
    }
    // The closest thing we have to "this slot is printing right now": there
    // is no active-extruder field in /api/state, but the ACE reports the slot
    // its feed assist is armed on, and the printing lane keeps FA armed the
    // whole print (CLAUDE.md 12 - the armed slot is the strongest
    // which-slot-feeds-this-head signal). -1 = nothing armed.
    function panelSlotActive(ace, slotIdx) {
      return !!ace && ace.feed_assist === slotIdx;
    }
    // Bottom line of a card. AFC prints the colour name there; our closest
    // equivalent is the spool's own name, else whatever brand the slot
    // reports. Empty string keeps the row (and thus the card height) stable.
    function panelSlotLabel(aceIdx, slotIdx) {
      const sp = spoolForSlot(aceIdx, slotIdx);
      if (sp && sp.label) return sp.label;
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      return (sl && sl.brand) || "";
    }
    // Is this slot's filament moving right now - the card blinks green while
    // loading, red while unloading, same signal as the dashboard's toolhead
    // card. head_source is what ties a SLOT card to a per-HEAD operation: it
    // is stamped BEFORE the feed and cleared only on a verified unload, so
    // the card that blinks is the one whose filament actually moves - in both
    // modes, and for a combiner slot whose index does not match its head.
    // toolheadOps is declared further down; this only runs at render time.
    function panelSlotOp(aceIdx, slotIdx) {
      const head = panelSlotHeadLoaded(aceIdx, slotIdx);
      return head === null ? null : (toolheadOps.value[head] || null);
    }
    // Content of the thumbnail line. In an all-cameras strip a tile is about
    // 190px wide, where four slot cards are neither readable nor tappable -
    // so below that the stylesheet swaps the cards for this: the lane that is
    // running, in its colour. WHEN that happens is decided in CSS alone (the
    // iframe is the viewport, so a media query sees the tile), which is why
    // this is a plain computed with no resize listener behind it.
    const panelMini = computed(() => {
      // Same signal as panelSlotActive - /api/state has no active-extruder
      // field, but the printing lane keeps feed assist armed for the whole
      // print. The shown unit is asked first, then the others: a swap can
      // leave the pinned card idle while a different unit is the one feeding.
      const aces = state.aces || [];
      const shown = panelAce.value;
      const order = shown ? [shown].concat(aces.filter(a => a !== shown)) : aces;
      for (const a of order) {
        const s = a.feed_assist;
        if (typeof s !== "number" || s < 0) continue;
        const slot = (a.slots || []).find(x => x.idx === s) || {};
        const head = panelSlotHeadLoaded(a.idx, s);
        return {
          color: slot.color || null,
          // The head is what you look for; if the armed slot feeds none
          // (possible between a swap's unload and load), name the unit
          // instead of inventing a head.
          main: head === null ? "ACE " + dispIdx(a.idx) : "T" + dispIdx(head),
          sub: slot.material || "",
        };
      }
      // Nothing armed: idle, or a print that has not reached its first load.
      return {color: null,
              main: shown ? "ACE " + dispIdx(shown.idx) : "multiACE", sub: ""};
    });
    // Ask back before anything that moves filament, when the printer has
    // "confirm commands" on. Deliberately the native confirm(): it blocks,
    // so a mis-click cannot slip past while the dialog renders, and it needs
    // no state of its own. Off -> always true, i.e. zero change.
    function _confirmCmd(key, params) {
      if (!state.confirm_commands) return true;
      return window.confirm(t(key, params || {}));
    }
    function loadAll(idx) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.load_all", {ace: dispIdx(idx)})) return;
      run("ACE_SWITCH", {TARGET: idx, AUTOLOAD: 1});
    }
    function _phaseFor(channelState) {
      if (!channelState) return null;
      const s = String(channelState);
      if (s.endsWith('_finish') || s.endsWith('_fail')) return null;
      if (s === 'wait_insert' || s === 'inited' || s === 'test') return null;
      if (s.startsWith('unload_')) return 'unloading';
      if (s.startsWith('load_'))   return 'loading';
      if (s.startsWith('preload_')) return 'loading';
      if (s.startsWith('manual_sta_')) return 'loading';
      return null;
    }
    const toolheadOps = computed(() => {
      const ops = {};
      for (const t of state.toolheads) {
        const p = _phaseFor(t.channel_state);
        if (p) ops[t.idx] = p;
      }
      return ops;
    });
    // During an ACTIVE print, a user-initiated load/unload from the dashboard
    // would interleave its homing/moves into the running motion queue and ruin
    // the print: Klipper is single-threaded, but gcode injected via Moonraker
    // runs BETWEEN the SD-print lines, it is NOT ignored. The print's OWN swaps
    // (ACE_SWAP_HEAD from the gcode file) and runout-reloads do not go through
    // these buttons, so block the buttons here instead of in the engine (a
    // command-level block would also kill the print's own swap). Gated on
    // 'printing' ONLY - a PAUSED print still needs Load for runout recovery
    // (see needsReload). Drying the other ACE mid-print is a wanted feature and
    // stays enabled (the FA-preserve in _perform_switch + the V2 watchdog keep
    // the printing head fed).
    const isPrinting = computed(() => state.printer_state === 'printing');
    function _blockIfPrinting() {
      if (isPrinting.value) {
        setMacroLog(t("ui.dashboard.blocked_printing"));
        return true;
      }
      return false;
    }
    // Does this slot have filament at the ACE input? 'empty' is the backend's
    // own verdict (gate == 0 or an empty status, V1's 'empty1' included);
    // 'unknown' means the unit has not reported yet and must NOT block
    // anything. The engine refuses to load an empty slot - it has since the
    // very first version - so offering the button here only ever produced
    // the useless half of the sequence: on an occupied head loadSlot
    // enqueues unload+load, the unload ran, the load was refused, and the
    // head stood empty for nothing (HW 2026-08-03, Dirk).
    function slotIsEmpty(aceIdx, slotIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      return !!sl && sl.state === 'empty';
    }
    function isToolheadOccupied(aceIdx, slotIdx) {
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (!th) return false;
      if (th.head_source_known) {
        if (th.ace !== aceIdx) return false;
        // Multi twin of slotLoadedInHead's sensor rule: a head whose
        // toolhead sensor explicitly reads CLEAR is not really occupied -
        // head_source is retry/FA bookkeeping, not filament (failed load,
        // manual extraction: bowden off, lever, pull, cut, just load).
        // The engine's own already-loaded guard reads the same sensor.
        // toolheadOps keeps it "occupied" while a load/unload still RUNS
        // (the sensor clears ~30s before an unload finishes); a None/
        // undefined sensor stays conservative-occupied.
        if (th.filament_at_extruder === false && !toolheadOps.value[th.idx]) {
          return false;
        }
        return true;
      }
      return !!th.filament_at_extruder;
    }
    // Mid-print runout: print paused, the head still owns its ACE source
    // (head_source NOT cleared - that would break FA-rearm on resume), but the
    // toolhead motion sensor reads empty. ACE_LOAD_HEAD's "already loaded" guard
    // is gated on that same toolhead sensor, so a reload goes through during a
    // runout even though head_source is set. We just re-enable the load button.
    function needsReload(aceIdx, slotIdx) {
      if (state.printer_state !== 'paused') return false;
      // The slot to reload is the one a paused head is SOURCED from
      // (head_source ace/slot), not the toolhead whose index == the slot.
      // In head mode a head loads from any slot of its wired ACE, so the old
      // idx===slot lookup blinked "reload" under the wrong slot (slot==head).
      // toolheadOps gate: during a running unload the sensor clears ~30s
      // before head_source does - without the gate the button flipped to
      // "Reload" mid-op and invited a click into the half-finished unload
      // (Dirk 2026-07-26).
      return state.toolheads.some(th =>
        th.head_source_known &&
        th.ace === aceIdx && th.slot === slotIdx &&
        th.filament_at_extruder === false &&
        !toolheadOps.value[th.idx]);
    }
    function unloadHead(idx) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.unload_head", {head: dispIdx(idx)})) return;
      run("ACE_UNLOAD_HEAD", {HEAD: idx});
    }
    function unloadAll() {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.unload_all")) return;
      run("ACE_UNLOAD_ALL_HEADS");
    }
    // The three head-mode setters used to swallow the outcome entirely
    // (empty catch, no res.ok, fire-and-forget reload). A REFUSAL from the
    // engine - "unload it first", the 1:1 wiring collision - was therefore
    // invisible: the checkbox had already flipped in the DOM, the state said
    // otherwise, and nothing explained why. Same lesson as spoolMacro, so the
    // same shape: report the reason, and AWAIT the reload so the controls are
    // repainted from the truth before the caller returns.
    async function headSet(path, body, label) {
      try {
        const r = await fetch(`${API}/${path}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const b = await r.json().catch(() => ({}));
          const detail = String(b.detail || `HTTP ${r.status}`);
          const m = detail.match(/[Ee]rror on '[^']*':\s*(.*)/);
          setMacroLog(`${label}: ${(m ? m[1] : detail).slice(0, 200)}`);
        }
      } catch (e) {
        setMacroLog(`${label}: ${e}`);
      }
      await reloadState();
    }
    // A checkbox bound with :checked (not v-model) KEEPS the user's click in
    // the DOM when the action is refused: the bound value never changed, so
    // Vue has nothing to patch, and the box shows the opposite of the truth
    // with no way back but a page reload. HW 2026-08-09: a refused feeder
    // toggle left the checkmark gone while the state still said feeder - so
    // the ACE picker (v-if="!t_.feeder") stayed correctly hidden and it read
    // as "the checkbox did something and then nothing appeared".
    // Reverting the DOM right away makes the box purely state-driven: it
    // moves only once the state has actually moved.
    function headToggle(ev, current, fn) {
      const wanted = ev.target.checked;
      ev.target.checked = current;
      return fn(wanted);
    }
    async function setHeadManual(idx, enable) {
      await headSet("head-manual", {head: idx, enable: !!enable},
                    t("ui.dashboard.manual"));
    }
    async function setHeadFeeder(idx, enable) {
      await headSet("head-feeder", {head: idx, enable: !!enable},
                    t("ui.dashboard.feeder"));
    }
    // Hybrid per-head mode: a Y-splitter joins the head's stock feeder onto
    // its ACE's path, so it can swap between its ACE slots and the feeder
    // spool mid-print (ACE_SWAP_HEAD ... SOURCE=FEEDER). Only meaningful on
    // a head that is already ACE-driven (head_uses_ace) - the backend
    // refuses it otherwise, same guard as the checkbox's v-if should apply.
    function headFeederComboOf(idx) {
      const hc = state.head_feeder_combo || {};
      return !!(hc[String(idx)] ?? hc[idx] ?? false);
    }
    async function setHeadFeederCombo(idx, enable) {
      await headSet("head-feeder-combo", {head: idx, enable: !!enable},
                    t("ui.dashboard.feeder_combo"));
    }
    // head mode: background-swap opt-in per head (= the HARDWARE declaration
    // "this head's dock is open below"). Engine-persisted (ace__bg_heads),
    // direct macro call like the language dropdown - no command queue entry.
    function bgEnabledFor(idx) {
      // Called from the render, so it must survive a state object that the
      // running app.js never initialised - see the template guards.
      return ((state.bg_swap || {}).enabled_heads || [])
        .some(h => Number(h) === Number(idx));
    }
    async function setBgHead(idx, enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_BG_SET_HEAD",
                                args: {HEAD: idx, ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Spoolman. The URL is edited locally and applied with its own button -
    // typing into a field that fires a macro per keystroke would spam the
    // printer and persist half-typed URLs.
    const spoolmanUrl = ref("");
    const spoolmanBusy = ref(false);
    const spoolmanLast = ref(null);
    // With a Spoolman URL configured, Spoolman is the ONLY source of new
    // spools (Dirk 2026-08-09: "entweder lokal oder spoolman" - the local
    // create paths hide, the pickers gain the search below). Without one,
    // everything stays local as before. Existing local-only entries keep
    // working either way - connecting must not orphan data.
    // World gate: the three-way switch decides (Dirk 2026-08-16), not
    // the URL - a configured URL with mode 'local' stays a local world.
    const spoolmanConnected = computed(() => state.spool_mode !== "local");
    // URL presence, for the connection probe: the ping must work BEFORE
    // the mode can be switched (spoolman/spoollink need a live check).
    const spoolmanUrlSet = computed(() =>
      !!(state.spoolman_url || "").trim());
    // Spoolman search-over-everything: the collection stays in Spoolman
    // (10k spools must never enter the table/state payload); the backend
    // filters and returns a top-50. Selecting a row ADOPTS that one spool
    // into the local table and, from a picker, selects it there.
    const smQuery = ref("");
    const smRows = ref([]);
    const smBusy = ref(false);
    const smOpen = ref(false);
    let _smTimer = null;
    function smSearchDebounced() {
      smOpen.value = true;
      clearTimeout(_smTimer);
      _smTimer = setTimeout(smSearch, 300);
    }
    async function smSearch() {
      smBusy.value = true;
      try {
        const r = await fetch(`${API}/spoolman/search?q=${
          encodeURIComponent(smQuery.value.trim())}`);
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.status);
        smRows.value = j.rows || [];
      } catch (e) {
        smRows.value = [];
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // Dismissing the result list. It used to close ONLY on a successful
    // adopt or by closing the whole picker (Dirk 2026-08-15: "kann sie nicht
    // mehr schliessen"). Escape and a click outside now do it too.
    // pointerdown, not blur: blur fires BEFORE the click on a result row, so
    // a blur-close would swallow the selection. And pointerdown inside the
    // .sm-search wrapper (both the tab's and the picker's carry that class)
    // is exactly the case to leave alone - the rows live in there.
    // The overlay stays untouched: its own @click.self already closes the
    // picker, which clears the search anyway, so the outcome is unchanged.
    function _smDocPointerDown(ev) {
      if (!smOpen.value) return;
      const t_ = ev && ev.target;
      if (t_ && t_.closest && t_.closest(".sm-search")) return;
      smOpen.value = false;
    }
    function _smDocKeydown(ev) {
      if (ev.key !== "Escape" || !smOpen.value) return;
      smOpen.value = false;
      // The picker has no Escape handler of its own, but stop here anyway so
      // one press never means "close the list AND the dialog".
      ev.stopPropagation();
    }
    // Binding is MANDATORY at adopt (Dirk: "zwingend die Bindung") - an
    // unbound Spoolman entry must never exist, then nothing ever needs a
    // cleanup pass. In a picker the target is the picker's own slot/head
    // and is bound IMMEDIATELY (a cancel after adopt would otherwise leave
    // an orphan); in the tab a result is STAGED (smPick) and the adopt bar
    // demands a target before the button arms.
    const smPick = ref(null);
    async function _smAdoptCore(row) {
      const r = await fetch(`${API}/spoolman/adopt`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({spoolman_id: row.spoolman_id}),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.status);
      return String(j.id);
    }
    async function smAdopt(row) {
      if (!picker.show) { smPick.value = row; return; }
      smBusy.value = true;
      try {
        const id = await _smAdoptCore(row);
        if (picker.head !== null && picker.head !== undefined) {
          await spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: picker.head, ID: id});
        } else {
          await spoolMacro("ACE_SPOOL_ASSIGN",
                           {ACE: picker.ace, SLOT: picker.slot, ID: id});
        }
        await reloadState();
        picker.spool = id;
        smOpen.value = false;
        smQuery.value = "";
        smRows.value = [];
      } catch (e) {
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // A search row whose spool is ALREADY bound somewhere is no pick -
    // the target-list discipline applied to the spool side (Dirk
    // 2026-08-09: "wenn ich aber eine vergebene waehle, darf ich dann
    // nicht speichern"): in the picker the click binds IMMEDIATELY, so a
    // taken row would displace the other binding or run into the engine's
    // red backstop. Bound to the CURRENT picker target stays clickable
    // (re-pick of what already sits here, the adopt then just refreshes).
    function smRowTaken(r) {
      const lid = r && r.local_id;
      if (!lid) return false;
      const key = spoolSlotKey((state.spools || {})[lid] || {});
      if (!key) return false;
      if (picker.show) {
        const own = (picker.head !== null && picker.head !== undefined)
          ? `h${picker.head}` : `${picker.ace}_${picker.slot}`;
        return key !== own;
      }
      return true;
    }
    // Where the taken spool sits - shown in the greyed row so the grey
    // explains itself instead of looking broken.
    function smRowWhere(r) {
      const lid = r && r.local_id;
      const sp = lid ? (state.spools || {})[lid] : null;
      return sp ? spoolSlotLabel(sp) : "";
    }
    // A Spoolman row is not edited locally - its identity lives in
    // Spoolman, a local edit would create two truths (the S41 write-back
    // rule). The ONE manual correction that stays is the weight against a
    // scale (Dirk 2026-08-09: "hoechstens manuelle anpassung gewicht"),
    // as a small dialog instead of the full editor form.
    function spoolWeightDialog(sp) {
      confirm({
        title: spoolTitle(sp),
        message: t("ui.spools.weight_dlg_msg"),
        inputLabel: t("ui.spools.weight"),
        inputValue: (sp.weight_g === undefined || sp.weight_g === null)
          ? "" : String(Math.round(sp.weight_g)),
        validate: v => {
          const n = Number(String(v).trim());
          return (String(v).trim() !== "" && Number.isFinite(n)
                  && n >= 0 && n <= 10000)
            ? "" : t("ui.spools.weight_dlg_bad");
        },
        okLabel: t("ui.common.save"),
        onOk: ({value}) => {
          spoolMacro("ACE_SPOOL_SET",
                     {ID: sp.id, WEIGHT: Number(String(value).trim())});
        },
      });
    }
    const smPickTarget = ref("");
    async function smAdoptStaged() {
      const row = smPick.value;
      const key = smPickTarget.value;
      if (!row || !key) return;
      smBusy.value = true;
      try {
        const id = await _smAdoptCore(row);
        if (key.startsWith("h")) {
          await spoolMacro("ACE_SPOOL_ASSIGN",
                           {HEAD: Number(key.slice(1)), ID: id});
        } else {
          const [a, sl] = key.split("_").map(Number);
          await spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl, ID: id});
        }
        smPick.value = null;
        smPickTarget.value = "";
        smOpen.value = false;
        smQuery.value = "";
        smRows.value = [];
      } catch (e) {
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // Badge for a bound spool: Spoolman-backed shows "SM" in the Spoolman
    // orange (Dirk), a purely local spool keeps the green SPULE badge.
    // Empty feeder/manual head: the tile must not wear the declared colour
    // as its SURFACE (a white identity on an empty feeder is
    // indistinguishable from loaded white, Dirk 2026-08-09) - it goes grey
    // with the colour demoted to the border. Explicit false only: an
    // unknown sensor keeps the colour rather than guessing "empty".
    function headTileEmpty(t_) {
      if (!t_) return false;
      if (t_.feeder) return t_.filament_detected === false;
      if (t_.manual) return t_.filament_at_extruder === false;
      // combo_feeder: t_.filament_detected mirrors the ACE gate while the
      // head is ACE-routed, so it can't tell "feeder tap empty" apart from
      // "ACE side empty" here - filament_in_toolhead is the feeder port's
      // own raw sensor, unaffected by which side is currently routed.
      if (t_.combo_feeder) return t_.filament_in_toolhead === false;
      return false;
    }
    function spoolBadgeCls(sp) {
      return sp && sp.spoolman_id ? "src-spoolman" : "src-spool";
    }
    function spoolBadgeLabel(sp) {
      // "SL" in the SpoolLink world, "SM" in plain Spoolman (Dirk
      // 2026-08-16: "dann sieht man gleich wo man ist") - same orange,
      // the letters alone carry the mode.
      if (sp && sp.spoolman_id) return state.spoollink ? "SL" : "SM";
      return t("ui.common.source_spool");
    }
    // The REAL connection state, probed - null while unknown, so the
    // checkmark can only ever appear after the instance actually answered
    // (Dirk: "kann der nur bei aktiver Verbindung erscheinen?"). The URL
    // watcher below re-probes on every URL change including the initial
    // state load; clicking the indicator re-checks by hand.
    const smPing = ref(null);
    const smPingInfo = ref("");
    async function spoolmanPing() {
      if (!spoolmanUrlSet.value) { smPing.value = null; return; }
      smPing.value = null;
      try {
        const r = await fetch(`${API}/spoolman/ping`);
        const j = await r.json().catch(() => ({}));
        smPing.value = !!j.ok;
        smPingInfo.value = j.ok ? (j.version || "") : (j.reason || "");
      } catch (e) {
        smPing.value = false;
        smPingInfo.value = String(e);
      }
    }
    watch(() => state.spoolman_url, (v) => {
      if (!spoolmanBusy.value) spoolmanUrl.value = v || "";
      spoolmanPing();
    }, {immediate: true});
    const spoolmanStatusText = computed(() => {
      const r = spoolmanLast.value;
      if (spoolmanBusy.value) return t("ui.spools.spoolman_running");
      if (!r) return "";
      if (r.error) return `${t("ui.common.error")}: ${r.error}`;
      return t("ui.spools.spoolman_result",
               {pulled: r.pulled || 0, pushed: r.pushed || 0})
             + (r.msg ? ` (${r.msg})` : "");
    });
    async function saveSpoolmanUrl() {
      const wasConnected = spoolmanUrlSet.value;
      const nowConnected = !!spoolmanUrl.value.trim();
      await spoolMacro("ACE_SET_SPOOLMAN", {URL: spoolmanUrl.value.trim()});
      spoolmanPing();
      // Entering the Spoolman world: adopt+bind every occupied slot whose
      // tag names a Spoolman spool (SM<id> scheme) - the counterpart of
      // the Klipper-side rebind that restores LOCAL bindings on the way
      // back (Dirk 2026-08-09: "sonst muss ich alle spulen neu einlegen"
      // / "auch beim wechseln zu spoolman"). Convenience sweep: a failure
      // just leaves the manual search+adopt path.
      if (nowConnected && !wasConnected) {
        try {
          const r = await fetch(`${API}/spoolman/adopt_by_tags`,
                                {method: "POST"});
          const body = await r.json().catch(() => ({}));
          if (body.adopted)
            setMacroLog(t("ui.spools.tag_adopted", {n: body.adopted}));
          if (body.errors && body.errors.length)
            setMacroLog(`Spoolman: ${body.errors.join("; ")}`);
        } catch (e) { /* sweep only; search+adopt remains */ }
        await reloadState();
      }
    }
    async function setSpoolmanAuto(enable) {
      await spoolMacro("ACE_SET_SPOOLMAN", {AUTO: enable ? 1 : 0});
    }
    // --- ACE 2 firmware update (Config tab; flash engine based on
    // hakimio's OTA updater, DEV-pending his license OK). The heavy
    // lifting is Klipper (port release/hold) + backend (flash thread);
    // this is upload, two buttons and a poll. The flash button goes
    // through the BIG RED own-risk dialog (Dirk 2026-08-09). ---
    const acefw = reactive({ace: "", version: "", password: "",
                            fileName: "", fileSize: 0, busy: false,
                            status: null, uiError: "", force: false});
    // Tested-versions allowlist (Dirk: "nur getestete Versionen") - the
    // version SELECT offers exactly these, the byte gate lives in the
    // backend. Loaded once at mount; empty list = flashing impossible,
    // the dry run stays open (it is the release tool that produces the
    // CRC/MD5 for a NEW entry).
    const acefwVersions = ref([]);
    async function acefwLoadVersions() {
      try {
        const r = await fetch(`${API}/acefw/versions`);
        const b = await r.json().catch(() => ({}));
        acefwVersions.value = b.versions || [];
      } catch (e) { /* leave empty */ }
    }
    const acefwInput = ref(null);
    const acefwCandidates = computed(() =>
      (state.aces || [])
        .filter(a => (a.protocol || "").toLowerCase() === "v2")
        .map(a => ({value: a.idx,
                    label: "ACE " + dispIdx(a.idx)
                           + (a.firmware && a.firmware !== "Unknown"
                              ? " · " + a.firmware : "")
                           + (a.connected ? "" : " (offline)")})));
    function acefwPickFile() { acefwInput.value && acefwInput.value.click(); }
    async function acefwUpload(files) {
      const f = files && files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch(`${API}/acefw/upload`, {method: "POST", body: fd});
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
        acefw.fileName = body.name;
        acefw.fileSize = body.size;
        acefw.uiError = "";
        // Pre-select the version from the file name - but only when it is
        // a TESTED one (the field is a select over the allowlist now; an
        // unknown guess would silently create an invalid selection).
        if (body.version_guess && !acefw.version.trim()
            && acefwVersions.value.some(v => v.version === body.version_guess))
          acefw.version = body.version_guess;
      } catch (e) { acefw.uiError = `Upload: ${e.message || e}`; }
    }
    let _acefwTimer = null;
    async function _acefwPoll() {
      try {
        const r = await fetch(`${API}/acefw/status`);
        acefw.status = await r.json();
      } catch (e) { /* next poll */ }
      const st = acefw.status && acefw.status.state;
      if (st === "done" || st === "error" || st === "idle") {
        if (_acefwTimer) { clearInterval(_acefwTimer); _acefwTimer = null; }
        acefw.busy = false;
        reloadState();
      }
    }
    async function _acefwStart(dry) {
      acefw.busy = true;
      acefw.uiError = "";
      acefw.status = {state: dry ? "starting dry run" : "starting",
                      pct: null, msg: "sending request…"};
      try {
        const r = await fetch(`${API}/acefw/flash`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ace: Number(acefw.ace),
                                version: acefw.version.trim(),
                                password: acefw.password || null,
                                dry_run: !!dry,
                                // Force skips only the "already on this
                                // version" check - the allowlist byte
                                // gate stays. For the re-flash test.
                                force: !!acefw.force}),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          // 404 here = the backend has no acefw routes = it was not
          // restarted (a stale uvicorn still holds the port). Name that
          // explicitly instead of a bare "HTTP 404".
          if (r.status === 404)
            throw new Error("backend has no firmware routes — restart the "
                            + "multiace-web service (a stale process may "
                            + "still hold the port)");
          throw new Error(body.detail || `HTTP ${r.status}`);
        }
        if (!_acefwTimer) _acefwTimer = setInterval(_acefwPoll, 1500);
      } catch (e) {
        acefw.busy = false;
        acefw.status = null;
        acefw.uiError = `${e.message || e}`;
      }
    }
    // The dry run needs no version (it only reads the CURRENT one), so it
    // is available as soon as an ACE + file are chosen; the real flash
    // additionally needs the target version. Splitting the two is what
    // fixes "nothing happens" - the version field no longer silently
    // disables the Testlauf (Dirk 2026-08-09).
    function acefwCanTest() {
      return acefw.ace !== "" && !!acefw.fileName && !acefw.busy;
    }
    function acefwReady() {
      return acefwCanTest() && !!acefw.version.trim();
    }
    function acefwTest() { _acefwStart(true); }
    function acefwFlash() {
      confirm({
        title: t("ui.config.acefw_confirm_title"),
        message: `<div class="acefw-danger">${t("ui.config.acefw_confirm_msg",
                  {ace: dispIdx(Number(acefw.ace)),
                   version: acefw.version.trim()})}</div>`,
        okLabel: t("ui.config.acefw_confirm_ok"),
        onOk: () => _acefwStart(false),
      });
    }
    const acefwStatusText = computed(() => {
      if (acefw.uiError) return `${t("ui.common.error")}: ${acefw.uiError}`;
      const s = acefw.status;
      if (!s || s.state === "idle") return "";
      if (s.state === "error")
        return `${t("ui.common.error")}: ${s.error || "?"}`;
      if (s.state === "done") {
        const res = s.result || {};
        if (res.dry_run) {
          const conn = res.connected
            ? t("ui.config.acefw_test_conn", {current: res.current || "?"})
            : t("ui.config.acefw_test_noconn");
          const img = res.image_ok
            ? t("ui.config.acefw_test_img",
                {size: res.size || 0, crc: res.crc || "?",
                 md5: res.md5 || "?", source: res.source || "?"})
            : t("ui.config.acefw_test_noimg", {reason: res.image_error || "?"});
          return `${conn} · ${img}`;
        }
        if (res.skipped)
          return t("ui.config.acefw_skipped", {v: res.current || "?"});
        return t("ui.config.acefw_done",
                 {v: res.new || res.target || "?"});
      }
      const pct = (s.pct === null || s.pct === undefined)
        ? "" : ` ${Math.round(s.pct)}%`;
      return `${s.state}${pct} — ${s.msg || ""}`;
    });
    async function spoolmanSync(pull, push) {
      if (spoolmanBusy.value) return;
      spoolmanBusy.value = true;
      spoolmanLast.value = null;
      try {
        const r = await fetch(`${API}/spoolman/sync`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({pull: !!pull, push: !!push}),
        });
        const body = await r.json().catch(() => ({}));
        spoolmanLast.value = r.ok ? body
          : {error: body.detail || `HTTP ${r.status}`};
      } catch (e) {
        spoolmanLast.value = {error: String(e)};
      } finally {
        spoolmanBusy.value = false;
        reloadState();
      }
    }
    // Humidity control per ACE 2. One field per call - the command merges
    // into the stored settings, so a partial update is exactly right here.
    // Values are CLAMPED before they go out: a number field a user typed
    // into without selecting its content produces 450 instead of 50, and a
    // typo should not become a printer-side error popup.
    const _AUTO_DRY_RANGE = {RH_START: [5, 95], RH_END: [1, 94],
                             TEMP: [35, 80], ADD_TIME: [0, 600]};
    // Master is an ACE INDEX (-1 = none), not a number to clamp into a range.
    // `state` is a reactive object, NOT a ref - state.value was undefined
    // here, and the TypeError killed the whole dry-panel render on every
    // ACE Pro card (the v2 block never calls this, so ACE 2 looked fine).
    // HW 2026-08-02, Dirk: "die ACE 1 karten zeigen keine dry funktionen
    // mehr an, komplett leer".
    const autoDryMasters = () => state.auto_dry_masters || [];
    // Edit buffer. The inputs are bound to PRINTER state, and the state is
    // re-polled every few seconds - so a refresh landing mid-edit threw the
    // typed value away, and a refresh landing between the send and the new
    // state made the field jump back to the old number (Dirk: "mal geht es
    // zurück, mal springt es auf 40, dann 45, dann 40"). While a field is
    // being edited its buffer wins; it is released only once the printer
    // reports the new value, so there is no window where the old one shows.
    const autoDryEdit = reactive({});
    const _adKey = (idx, param) => `${idx}_${param}`;
    function autoDryValue(idx, param, live) {
      const k = _adKey(idx, param);
      return (k in autoDryEdit) ? autoDryEdit[k] : live;
    }
    // Fields the printer REFUSED. Same treatment as a locally-invalid pair:
    // the frame turns red and the entered value stays, instead of a popup
    // over a snapped-back field. Cleared as soon as the user edits again or
    // the value is accepted.
    const autoDryErr = reactive({});
    function autoDryFieldError(idx, param) {
      return !!autoDryErr[_adKey(idx, param)];
    }
    function autoDryInput(idx, param, ev) {
      autoDryEdit[_adKey(idx, param)] = ev.target.value;
      delete autoDryErr[_adKey(idx, param)];
    }
    // The threshold pair is only ever wrong TOGETHER, so it is validated
    // here and not on the printer: an error popup for a half-typed number is
    // noise, and the old round-trip snapped the field back to the stored
    // value, throwing the edit away. Instead both fields go red, the typed
    // value stays, and nothing is sent until the pair makes sense again.
    function autoDryPairInvalid(idx, live) {
      if (!live) return false;
      const s = Number(autoDryValue(idx, 'RH_START', live.rh_start));
      const e = Number(autoDryValue(idx, 'RH_END', live.rh_end));
      if (!Number.isFinite(s) || !Number.isFinite(e)) return true;
      return e >= s;
    }
    async function autoDryCommit(idx, param, ev, live) {
      const k = _adKey(idx, param);
      autoDryEdit[k] = ev.target.value;
      // Keep the buffer (and the red frame) instead of sending a pair the
      // printer would only reject. Scoped to the two threshold fields on
      // purpose: temp / run-on are independent of the pair, and blocking
      // them here would strand the ACE Pro card if the [ace] defaults
      // themselves ever carried a bad pair.
      if ((param === 'RH_START' || param === 'RH_END')
          && autoDryPairInvalid(idx, live)) return;
      const ok = await setAutoDry(idx, {[param]: ev.target.value});
      if (!ok) {
        // Keep the buffer AND frame the field: the printer said no, so the
        // value the user is looking at is the one to fix.
        autoDryErr[k] = true;
        return;
      }
      // Release only now: spoolMacro AWAITS the state refresh, so the next
      // render already carries the accepted value and the field cannot fall
      // back through the old one on its way there.
      delete autoDryEdit[k];
    }
    // Enabling with an invalid pair would arm the OLD stored thresholds while
    // the card shows the new ones - so it is refused too, and the checkbox is
    // put back at once rather than flickering until the next poll.
    async function autoDryEnable(idx, ev, live) {
      const on = ev.target.checked;
      if (on && autoDryPairInvalid(idx, live)) {
        ev.target.checked = false;
        return;
      }
      const ok = await setAutoDry(idx, {ENABLE: on ? 1 : 0});
      if (!ok) ev.target.checked = !on;
    }
    // Follower master. A refusal here is what blocks its enable, so the
    // dropdown is the field to frame.
    async function autoDrySetMaster(idx, ev) {
      const k = _adKey(idx, 'MASTER');
      delete autoDryErr[k];
      const ok = await setAutoDry(idx, {MASTER: ev.target.value});
      if (!ok) autoDryErr[k] = true;
    }
    async function setAutoDry(idx, args) {
      const out = {};
      for (const [k, v] of Object.entries(args)) {
        const r = _AUTO_DRY_RANGE[k];
        if (!r) { out[k] = v; continue; }
        const n = Number(v);
        if (!Number.isFinite(n)) continue;
        out[k] = Math.min(r[1], Math.max(r[0], Math.round(n)));
      }
      if (!Object.keys(out).length) return true;
      return await spoolMacro("ACE_SET_AUTO_DRY",
                              Object.assign({ACE: idx}, out));
    }
    async function setConfirmCommands(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_CONFIRM_COMMANDS",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    async function setRememberFilament(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_REMEMBER_FILAMENT",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Register the panel as a camera in Fluidd. A button, not something
    // the installer does - it changes the user's own dashboard, and the
    // backend leaves an existing entry of that name alone, so a second
    // click cannot overwrite a URL or aspect ratio they adjusted.
    const fluiddCamBusy = ref(false);
    const fluiddCamMsg = ref("");
    async function addFluiddCamera() {
      fluiddCamBusy.value = true;
      fluiddCamMsg.value = "";
      try {
        const r = await fetch(`${API}/fluidd-camera`, {method: "POST"});
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || r.status);
        fluiddCamMsg.value = j.existed
          ? t("ui.config.fluidd_panel_exists")
          : t("ui.config.fluidd_panel_added");
      } catch (e) {
        fluiddCamMsg.value = `${t("ui.common.error")}: ${e.message || e}`;
      } finally {
        fluiddCamBusy.value = false;
      }
    }
    async function setPickupCleaning(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PICKUP_CLEANING",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    async function setAirprintDetection(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_AIRPRINT_DETECTION",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // ---- spool table ------------------------------------------------------
    // Klipper owns the table; every edit goes through gcode (/api/macro), the
    // UI never writes the file - one writer, no lost updates.
    // Starts EMPTY on purpose: the row doubles as a live FILTER for the
    // table while not editing (Dirk), so pre-filled defaults would hide
    // entries before the user typed anything. Add uses fallbacks instead.
    // Colour empty as well: the swatch fills it on first interaction, so an
    // untouched row filters on nothing. A new spool is NOT bound to a slot -
    // binding happens in the row's slot dropdown or from the slot card.
    const spoolForm = reactive({material: "", color: "", vendor: "",
                               subtype: "", weight: "", label: "", sku: "",
                               price: "20"});
    // The RFID tag travels to Klipper as a gcode argument, and '#' TERMINATES
    // gcode arguments - so the marker '#' many users put in front of a
    // self-written tag must be stripped here. Harmless: the matcher compares
    // canonically and ignores a leading '#' on either side.
    function _skuArg(s) {
      return String(s || "").trim().replace(/^#+/, "");
    }
    // Mirror of ace.py _sku_canon - comparison form only, the stored value
    // stays what the user typed. Keep the two in step: a looser check here
    // would offer a code the printer then refuses.
    function _skuCanon(s) {
      return String(s || "").trim().replace(/^#+/, "").trim().toLowerCase();
    }
    // Which spool already holds this code, or null. `exceptId` lets an EDIT
    // keep its own code without reporting a collision with itself.
    function _skuHolder(sku, exceptId) {
      const want = _skuCanon(sku);
      if (!want) return null;                    // no code = never a collision
      for (const [id, sp] of Object.entries(state.spools || {})) {
        if (exceptId != null && String(id) === String(exceptId)) continue;
        if (_skuCanon(sp && sp.sku) === want) return id;
      }
      return null;
    }
    // Same semantics as ace.py _spool_unique_sku, INCLUDING the free case:
    // a code nobody holds comes back unchanged. Cross-checked against the
    // real Python function; returning base+"_2" for a free code would be a
    // silent divergence the next caller inherits.
    function _skuSuggest(sku) {
      const base = String(sku || "").trim();
      if (!base || !_skuHolder(base)) return base;
      let n = 2;
      while (_skuHolder(base + "_" + n)) n++;
      return base + "_" + n;
    }
    // The tag code of a spool being created is already taken (the second
    // factory spool of an article - factory tags are per ARTICLE, not per
    // spool). Ask instead of deciding: the suffix is only a PROPOSAL in an
    // editable field, because the useful answer is usually a code the user
    // then writes onto the tag himself, which restores auto-binding - the
    // suffixed entry can never be recognised (its tag still reads the
    // original). Taking the code AWAY from the other spool is deliberately
    // impossible (Dirk 2026-08-09: "niemals überschreiben von sku zulassen"),
    // so the validation blocks, it does not warn. Resolves to the chosen code
    // or null when cancelled; an empty code is allowed and means "no tag".
    function askFreeSku(sku) {
      return new Promise(resolve => {
        const holder = _skuHolder(sku);
        if (!holder) return resolve(_skuArg(sku));
        const other = (state.spools || {})[holder] || {};
        confirm({
          title: t("ui.spools.sku_taken_title"),
          message: t("ui.spools.sku_taken_msg",
                     {sku: _skuArg(sku), id: holder, spool: spoolTitle(other)}),
          inputLabel: t("ui.spools.sku"),
          // Pre-strip the '#': it never survives to the table (gcode cuts at
          // '#'), so showing it would mean the field lies about what is
          // stored.
          inputValue: _skuArg(_skuSuggest(sku)),
          inputHint: t("ui.spools.sku_taken_hint"),
          validate: v => {
            const h = _skuHolder(v);
            return h ? t("ui.spools.sku_still_taken", {id: h}) : "";
          },
          okLabel: t("ui.spools.add"),
          onOk: ({value}) => resolve(_skuArg(value)),
          onCancel: () => resolve(null),
        });
      });
    }
    // Option lists for the spool form. Same sources as the slot picker (the
    // firmware filament DB via /api/materials + [ace_tipform] vendors), PLUS
    // whatever the existing spools already use - so the lists GROW with the
    // table instead of forcing free text for a vendor/subtype the printer
    // does not ship (Dirk: "Vendorliste aus default + spool liste").
    function _mergeCase(base, extra) {
      const out = [...base];
      const low = new Set(out.map(x => String(x).toLowerCase()));
      for (const e of extra) {
        const v = String(e || "").trim();
        if (v && !low.has(v.toLowerCase())) { out.push(v); low.add(v.toLowerCase()); }
      }
      return out;
    }
    const spoolMaterials = computed(() =>
      _mergeCase(pickerMaterials.value,
                 spoolList().map(sp => sp.material)));
    const spoolVendors = computed(() => {
      // No material chosen yet (the row is a filter then): offer the vendors
      // of ALL materials instead of an empty list.
      const byMat = spoolForm.material
        ? (pickerDb.value[spoolForm.material] || {})
        : Object.assign({}, ...Object.values(pickerDb.value || {}));
      const base = Object.keys(byMat);
      const withGeneric = base.includes("Generic")
        ? ["Generic", ...base.filter(x => x !== "Generic")]
        : ["Generic", ...base];
      const mat = String(spoolForm.material || "").toUpperCase();
      return _mergeCase(
        _mergeCase(withGeneric, tipformVendorsByMaterial.value[mat] || []),
        spoolList().filter(sp => !sp.material
                           || sp.material === spoolForm.material)
                   .map(sp => sp.vendor));
    });
    const spoolSubtypes = computed(() => {
      const byVendor = spoolForm.material
        ? (pickerDb.value[spoolForm.material] || {})
        : Object.assign({}, ...Object.values(pickerDb.value || {}));
      // Build the base through _mergeCase too: a DB that lists "Basic"
      // itself would otherwise show it twice (Basic = firmware 'generic').
      const base = _mergeCase(["Basic"], byVendor[spoolForm.vendor] || []);
      return _mergeCase(base, spoolList().map(sp => sp.subtype));
    });
    const spoolImportMode = ref("merge");
    const spoolFileInput = ref(null);
    // Same pattern as the gcode upload: the template calls the trigger,
    // the ref name matches the ref= attribute (setup() refs bind by name).
    function triggerSpoolImport() {
      spoolFileInput.value && spoolFileInput.value.click();
    }

    function spoolList() {
      return Object.values(state.spools || {})
        .sort((a, b) => (parseInt(a.id, 10) || 0) - (parseInt(b.id, 10) || 0));
    }
    // The tab redesign (Dirk 2026-08-09): the form is an EDITOR only (via
    // the row's pencil), never a filter and never a creator - new spools
    // come from the picker (local) or the Spoolman dialog. The list is
    // filtered by WORLD (connected -> only Spoolman-backed rows, local ->
    // only local rows; entries of the other world stay in the table but
    // out of sight) plus a free-text search over everything a row shows.
    const spoolFilterActive = computed(() => false);
    const spoolQuery = ref("");
    // The either/or worlds as ONE predicate (mirror of ace.py
    // _spool_in_world): with Spoolman connected only Spoolman-backed
    // entries exist for the UI, without it only local ones. Every surface
    // that OFFERS spools goes through this - the tab list did, the
    // picker's dropdown did not and kept offering the very local entries
    // the list hides (Dirk 2026-08-09: "die karten haben noch interne
    // spulen zur zuordnung im sm modus, entweder oder").
    function spoolWorldOk(sp) {
      return spoolmanConnected.value ? !!(sp && sp.spoolman_id)
                                     : !(sp && sp.spoolman_id);
    }
    // Sort of the tab list (Dirk 2026-08-09): every column header sorts
    // by ITS column - assigned (slot order, bound first: the list mirrors
    // the printer on top, the shelf below; the DEFAULT), label
    // (alphabetical), sku (tag codes, for scanning while writing tags),
    // weight (lightest first - what is about to run out sits on top,
    // unknown weights last).
    const spoolSort = ref("assigned");
    function _spoolBindOrder(sp) {
      const k = spoolSlotKey(sp);
      if (!k) return 9999;
      const m = /^(\d+)_(\d+)$/.exec(k);
      if (m) return Number(m[1]) * 10 + Number(m[2]);
      return k.startsWith("h") ? 800 + (Number(k.slice(1)) || 0) : 9998;
    }
    function _spoolTitleCmp(a, b) {
      return String(spoolTitle(a)).toLowerCase()
        .localeCompare(String(spoolTitle(b)).toLowerCase());
    }
    function spoolListShown() {
      let all = spoolList().filter(spoolWorldOk);
      const terms = spoolQuery.value.trim().toLowerCase().split(/\s+/)
        .filter(Boolean);
      if (terms.length) {
        all = all.filter(sp => {
          const hay = [sp.label, sp.vendor, sp.material, sp.subtype,
                       _skuArg(sp.sku), sp.color,
                       sp.spoolman_id ? "sm" + sp.spoolman_id : "",
                       spoolSlotLabel(sp)].join(" ").toLowerCase();
          return terms.every(t => hay.includes(t));
        });
      }
      const rows = [...all];
      if (spoolSort.value === "sku") {
        // Empty codes sort LAST - a tag scan looks for codes, not gaps.
        rows.sort((a, b) =>
          (_skuCanon(a.sku) || "￿")
            .localeCompare(_skuCanon(b.sku) || "￿")
          || _spoolTitleCmp(a, b));
      } else if (spoolSort.value === "label") {
        rows.sort(_spoolTitleCmp);
      } else if (spoolSort.value === "weight") {
        // Unknown weight is "I don't know", not 0 g - it must not mix in
        // with the genuinely empty spools at the top. 1e12, not Infinity:
        // Infinity - Infinity is NaN and NaN poisons a sort comparator.
        const w = sp => (sp.weight_g === undefined || sp.weight_g === null)
          ? 1e12 : Number(sp.weight_g);
        rows.sort((a, b) => (w(a) - w(b)) || _spoolTitleCmp(a, b));
      } else {
        rows.sort((a, b) => (_spoolBindOrder(a) - _spoolBindOrder(b))
                            || _spoolTitleCmp(a, b));
      }
      return rows;
    }
    // '+' next to the search (Dirk 2026-08-09: "add spool button im mace
    // mode .. + neben der suche"): opens the empty form as a CREATOR -
    // local mode only, in Spoolman mode new spools come via search+adopt.
    // Save routes through spoolSave -> spoolAdd (unassigned entry,
    // collision dialog included) - that path stayed fully wired when the
    // tab redesign made the form edit-only, it was just unreachable.
    const spoolCreating = ref(false);
    function spoolNewForm() {
      spoolFormClear();
      spoolCreating.value = true;
    }
    function spoolFormClear() {
      spoolCreating.value = false;
      spoolEditId.value = "";
      spoolForm.material = ""; spoolForm.vendor = ""; spoolForm.subtype = "";
      spoolForm.label = ""; spoolForm.weight = ""; spoolForm.color = "";
      spoolForm.sku = ""; spoolForm.price = "20";
    }
    function spoolIdForSlot(aceIdx, slotIdx) {
      return (state.spool_binding || {})[`${aceIdx}_${slotIdx}`] || null;
    }
    function spoolForSlot(aceIdx, slotIdx) {
      const id = spoolIdForSlot(aceIdx, slotIdx);
      return id ? (state.spools || {})[id] || null : null;
    }
    // Feeder/manual heads bind their spool to the HEAD ('h<n>', engine key
    // domain) - they have no ACE slot, the spool sits at the side feeder.
    function spoolForHead(headIdx) {
      const id = (state.spool_binding || {})[`h${headIdx}`] || null;
      return id ? (state.spools || {})[id] || null : null;
    }
    function spoolSlotLabel(sp) {
      const key = Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id);
      if (!key) return "";
      if (key.startsWith("h")) {
        const h = Number(key.slice(1));
        const th = (state.toolheads || []).find(t => t.idx === h) || {};
        return `T${dispIdx(h)} · ${t(th.manual ? "ui.dashboard.manual" : "ui.dashboard.feeder")}`;
      }
      const [a, sl] = key.split("_").map(Number);
      return `ACE ${dispIdx(a)} / ${dispIdx(sl)}`;
    }
    // Remaining weight is an estimate (extruded length x density), but it
    // renders WITHOUT a ~ (Dirk 2026-08-16: "kann das ca. zeichen weg -
    // machen die anderen auch nicht"; Spoolman/SpoolLink show plain grams).
    function spoolWeightLabel(sp) {
      if (!sp || sp.weight_g === undefined || sp.weight_g === null) return "";
      return `${Math.round(sp.weight_g)} g`;
    }
    // Label AND the details - a label like "Rolle links" must not hide what
    // the spool actually is (Dirk).
    function spoolDetails(sp) {
      const sub = sp.subtype && !/^(basic|generic)$/i.test(sp.subtype)
        ? sp.subtype : "";
      return [sp.vendor, sp.material, sub].filter(Boolean).join(" ");
    }
    function spoolTitle(sp) {
      const det = spoolDetails(sp);
      if (sp.label) return det ? `${sp.label} · ${det}` : sp.label;
      return det || `#${sp.id}`;
    }
    // Reports failures. It used to swallow everything, which is how a
    // REJECTED setting could look like a saved one: the input is bound to
    // the printer state, so it silently snapped back to the old value and
    // nothing said why (HW 2026-07-31: RH_START=450 refused, field showed
    // 45 again, auto-dry stayed off and nobody could see it).
    // The three-way world switch. Guarded client-side the same way the
    // options are disabled (connection / agent), so a stale button click
    // cannot select an impossible mode; Klipper validates again anyway.
    async function setSpoolMode(v) {
      if (!v || v === state.spool_mode) return;
      if (v !== "local" && smPing.value !== true) return;
      if (v === "spoollink" && !state.spoollink_agent) return;
      await spoolMacro("ACE_SET_SPOOLMAN", {MODE: v});
    }
    const spoolAddBusy = ref(false);
    async function spoolMacro(name, args) {
      // One ACE_SPOOL_ADD at a time: /api/macro blocks while the printer
      // runs long gcode (a swap holds the script queue ~150s), so every
      // extra + click piled up behind it and each created a fresh spool
      // minutes later (Dirk 2026-08-16: "5 neue spulen"). Later clicks
      // are refused with a notice instead of queued; SET/ASSIGN etc. are
      // idempotent and stay unguarded.
      if (name === "ACE_SPOOL_ADD") {
        if (spoolAddBusy.value) {
          setMacroLog(t("ui.spools.add_pending"));
          return false;
        }
        spoolAddBusy.value = true;
      }
      let ok = false;
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, args}),
        });
        ok = r.ok;
        if (!ok) {
          const body = await r.json().catch(() => ({}));
          const detail = String(body.detail || `HTTP ${r.status}`);
          // Klipper's error text carries the reason; strip the wrapper so
          // the banner shows the sentence, not a JSON envelope.
          const m = detail.match(/[Ee]rror on '[^']*':\s*(.*)/);
          setMacroLog(`${name}: ${(m ? m[1] : detail).slice(0, 200)}`);
        }
      } catch (e) {
        setMacroLog(`${name}: ${e}`);
      }
      // AWAITED on purpose. Fire-and-forget here is what made edited fields
      // flip: the caller released its edit buffer as soon as this returned,
      // the state was still the OLD one, so the field showed the previous
      // value for a moment and only then the accepted one. Callers may rely
      // on the state being current when this resolves.
      try {
        await reloadState();
      } finally {
        if (name === "ACE_SPOOL_ADD") spoolAddBusy.value = false;
      }
      return ok;
    }
    // Edit mode: the same form edits an existing entry (spoolEditId set).
    const spoolEditId = ref("");
    function spoolEdit(sp) {
      spoolCreating.value = false;
      spoolEditId.value = sp.id;
      spoolForm.material = sp.material || "PLA";
      spoolForm.vendor = sp.vendor || "Generic";
      spoolForm.subtype = sp.subtype || "Basic";
      spoolForm.color = sp.color ? "#" + String(sp.color).replace("#", "") : "";
      spoolForm.weight = (sp.weight_g === undefined || sp.weight_g === null)
        ? "" : Math.round(sp.weight_g);
      spoolForm.label = sp.label || "";
      spoolForm.sku = sp.sku || "";
      spoolForm.price = (sp.price_per_kg === undefined || sp.price_per_kg === null)
        ? "20" : sp.price_per_kg;
    }
    function spoolEditCancel() {
      spoolEditId.value = "";
      spoolForm.label = ""; spoolForm.weight = ""; spoolForm.sku = "";
      spoolForm.price = "20";
    }
    function spoolSave() {
      if (!spoolEditId.value) { spoolAdd(); return; }
      const args = {ID: spoolEditId.value,
                    MATERIAL: spoolForm.material || "PLA",
                    VENDOR: spoolForm.vendor || "Generic",
                    SUBTYPE: spoolForm.subtype || "Basic",
                    LABEL: spoolForm.label || "",
                    SKU: _skuArg(spoolForm.sku),                        // "" clears it
                    COLOR: (spoolForm.color || "").replace("#", "")};   // "" clears it
      if (spoolForm.weight !== "") args.WEIGHT = spoolForm.weight;
      if (spoolForm.price !== "") args.PRICE_PER_KG = spoolForm.price;
      spoolMacro("ACE_SPOOL_SET", args);
      spoolEditCancel();
    }
    async function spoolAdd() {
      const args = {MATERIAL: spoolForm.material || "PLA"};   // empty = filter cleared
      if (spoolForm.color) args.COLOR = spoolForm.color.replace("#", "");
      if (spoolForm.vendor) args.VENDOR = spoolForm.vendor;
      if (spoolForm.subtype) args.SUBTYPE = spoolForm.subtype;
      if (spoolForm.label) args.LABEL = spoolForm.label;
      if (spoolForm.weight !== "") args.WEIGHT = spoolForm.weight;
      args.PRICE_PER_KG = spoolForm.price !== "" ? spoolForm.price : "20";
      // Ask BEFORE sending when the code is taken; null = cancelled, and then
      // nothing is created and the form keeps what was typed.
      const sku = await askFreeSku(spoolForm.sku);
      if (sku === null) return;
      if (sku) args.SKU = sku;
      // No ACE/SLOT here: a fresh spool starts UNASSIGNED (the old defaults
      // silently bound every new entry to ACE 0 / Slot 0). Bind it in the
      // row's slot dropdown or from the slot card.
      await spoolMacro("ACE_SPOOL_ADD", args);
      // Clear the row after adding: it is the filter again, and a stale
      // filter would hide the entry that was just created. Only AFTER the
      // dialog resolved - clearing it up front would throw the input away
      // on a cancel.
      spoolFormClear();
    }
    // Every ACE/slot that is a REAL assignment target, as picker options.
    // Value is the internal "ace_slot" key (gcode takes 0-based), the label
    // shows display indices - the S4 index-base rule. `taken` flags a
    // target that already holds a spool: the markup greys it out (Dirk
    // 2026-08-09: "ausgrauen was vergeben ist"), except the row's own
    // current binding, which must stay pickable as the selected value.
    const spoolSlotOptions = computed(() => {
      const out = [];
      const bound = new Set(Object.keys(state.spool_binding || {}));
      // Head mode wires each ACE 1:1 to one head - an ACE whose head runs
      // as feeder/manual is not in use, so its slots are no targets ("wenn
      // head 4 als feeder laeuft, muss ich nicht aus der ace waehlen
      // koennen"). Multi keeps every unit: slot N of ANY ACE feeds head N.
      let usedAces = null;
      if (state.mode === "head") {
        usedAces = new Set();
        for (const th of state.toolheads || []) {
          if (!th.feeder && !th.manual) usedAces.add(Number(headAceOf(th.idx)));
        }
      }
      for (const a of state.aces || []) {
        if (usedAces && !usedAces.has(Number(a.idx))) continue;
        for (const sl of (a.slots || [])) {
          const key = `${a.idx}_${sl.idx}`;
          out.push({key, taken: bound.has(key),
                    label: `ACE ${dispIdx(a.idx)} / ${dispIdx(sl.idx)}`});
        }
      }
      // Feeder/manual heads take a spool too (bound to the head, 'h<n>') -
      // the side feeder has no reader, so this dropdown is THE way to bind.
      for (const th of state.toolheads || []) {
        if (th.feeder || th.manual) {
          const key = `h${th.idx}`;
          out.push({key, taken: bound.has(key),
                    label: `T${dispIdx(th.idx)} · ${t(th.manual ? "ui.dashboard.manual" : "ui.dashboard.feeder")}`});
        }
      }
      return out;
    });
    // Does a spool match what the slot currently declares (RFID or
    // override)? Only fields the spool actually carries are compared, so a
    // half-filled entry still matches on what it does know.
    // How well a spool fits what the dialog currently shows. ADDITIVE, not a
    // filter: a mismatch used to return 0 for everything, so "same material,
    // other colour" ranked identically to "nothing in common" and the list
    // order looked arbitrary. Material counts double - it is the harder fact
    // (the colour is a rendering of one value, and near-identical shades are
    // common). 3 = both, 2 = material, 1 = colour, 0 = neither.
    // Standard swatches for the colour fields. The touchscreen's own palette
    // lives in the closed screen firmware (the Klipper side carries no colour
    // list at all, not even in the filament DB), so this is OUR set of common
    // filament colours - it works like the display's picker, it is not the
    // same table. A swatch writes a DECLARED colour: '#000000' means black,
    // not "unknown" (S40) - clearing is what the spool form's x button is for.
    const FILAMENT_SWATCHES = [
      "#000000", "#ffffff", "#808080", "#c8c8c8",
      "#e02020", "#ff7800", "#f5d800", "#22a03c",
      "#0f6b2e", "#3aa0e0", "#1436c8", "#7b2fbe",
      "#ff5aa8", "#7a4a1e", "#d4af37", "#e8dcc0",
    ];
    function sameSwatch(a, b) { return _hex6(a) === _hex6(b); }
    // Colours this printer already knows: every spool of the table plus every
    // slot that reports one. Hitting one's OWN recurring colour exactly beats
    // re-mixing it by eye, and it costs no palette invention. Standard
    // swatches are filtered out so the row stays additional information.
    const knownColors = computed(() => {
      const std = new Set(FILAMENT_SWATCHES.map(_hex6));
      const out = [];
      const add = (c) => {
        const h = _hex6(c);
        if (!h || h.length !== 7 || std.has(h) || out.includes(h)) return;
        out.push(h);
      };
      for (const sp of Object.values(state.spools || {})) add(sp.color);
      for (const a of state.aces || []) for (const sl of (a.slots || [])) add(sl.color);
      return out.slice(0, 16);
    });
    function spoolMatchesSlot(sp, material, colorHex) {
      const hex = (c) => String(c || "").replace("#", "").toLowerCase();
      const eq = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
      let score = 0;
      if (sp.material && material && eq(sp.material, material)) score += 2;
      if (sp.color && colorHex && hex(sp.color) === hex(colorHex)) score += 1;
      return score;
    }
    // Spools offered in the slot picker. Entries matching what the slot
    // declares (RFID tag or override) come FIRST and are marked - the ACE
    // tag says WHAT is in the slot but carries no serial, so it can narrow
    // the choice, never make it (two identical rolls are indistinguishable
    // to it). Already-bound spools stay listed with their slot, since
    // picking one here moves it.
    const spoolPickerOptions = computed(() => {
      const mat = picker.material;
      const col = picker.color;
      const rows = spoolList().filter(spoolWorldOk).map(sp => {
        const at = spoolSlotLabel(sp);
        const w = spoolWeightLabel(sp);
        const score = spoolMatchesSlot(sp, mat, col);
        const bg = sp.color ? "#" + String(sp.color).replace("#", "") : "";
        return {id: sp.id, score, bg, fg: bg ? textOn(bg) : "",
                // The tick means FULLY matching (material AND colour): the
                // colour is visible in the row itself, the material is not,
                // so it marks the one thing the fill cannot show.
                label: (score === 3 ? "\u2713 " : "")
                       + `${spoolTitle(sp)}${w ? " \u00b7 " + w : ""}`
                       + (at ? ` (${at})` : "")};
      });
      // A spool bound to ANOTHER slot is not offered at all. Moving a roll
      // means taking it out first, and that frees it (the gate-empty
      // release) - so picking a bound one can only be a mis-pick, and it
      // used to silently move the spool and leave its old slot unbound
      // (HW 2026-08-02: green vanished from ACE 1 / Slot 4 during an
      // unrelated re-assign). Not merely sorted down: an option that must
      // never be chosen has no business being choosable. THIS slot's own
      // spool stays, or the dropdown would open with nothing selected and
      // saving would clear the binding.
      const here = (picker.head !== null && picker.head !== undefined)
        ? ((state.spool_binding || {})[`h${picker.head}`] || "")
        : (spoolIdForSlot(picker.ace, picker.slot) || "");
      const pick = rows.filter(r => r.id === here
                                    || !spoolSlotKey((state.spools || {})[r.id] || {}));
      // Sort by match quality only. The old "free spools first" key changed
      // whenever a binding changed, so the state poll re-ordered the list
      // UNDER an open dropdown; match quality depends on the picker fields,
      // i.e. only on what the user themselves edits.
      return pick.sort((a, b) => b.score - a.score);
    });
    // Weight as the form shows it: rounded grams, or "" when the spool has
    // none (and for "no spool" at all).
    function _spoolWeightField(sp) {
      if (!sp || sp.weight_g === undefined || sp.weight_g === null) return "";
      return String(Math.round(sp.weight_g));
    }
    // The button next to the grams field, in the two states the row can be
    // in. Bound spool -> correct ITS weight (the scale-check, same as the
    // table's "g"). No spool -> create one for this slot from what the
    // dialog already shows, incl. the tag's #ID, and bind it: a spool the
    // printer just read is then one click from being tracked, instead of
    // retyping the id in the Spools tab.
    // What the '+' would create, previewed on hover (Dirk 2026-08-09:
    // "die sku anzeigt beim mouse over ... dann auch gewicht und
    // zuordnung ... also nur bei rfid"): only for a slot with a READ tag -
    // the preview reads from the exact sources the create uses (slot tag
    // sku via _pickerSlot, the grams field, the picker target), so it can
    // never diverge from the click. No tag (incl. the head picker - a
    // side feeder has no reader) -> the plain label stays.
    function spoolNewTitle() {
      if (picker.spool) return t("ui.spools.correct_weight");
      const base = t("ui.spools.new_from_slot");
      const sl = _pickerSlot();
      const sku = (sl && sl.rfid_data && sl.rfid_data.sku)
        ? _skuArg(sl.rfid_data.sku) : "";
      if (!sku) return base;
      const parts = [sku];
      if (picker.weight !== "") parts.push(picker.weight + " g");
      const isHead = picker.head !== null && picker.head !== undefined;
      parts.push(isHead ? `T${dispIdx(picker.head)}`
                        : `ACE ${dispIdx(picker.ace)} / ${dispIdx(picker.slot)}`);
      return base + " · " + parts.join(" · ");
    }
    async function spoolCreateFromPicker() {
      const isHead = picker.head !== null && picker.head !== undefined;
      if ((picker.ace === null || picker.ace === undefined) && !isHead) return;
      if (picker.spool) {
        const sp = (state.spools || {})[picker.spool];
        // Empty is "I don't know", not "0 g" - and the table has no way to
        // unset a weight, so an emptied field is simply not sent. Otherwise
        // only on a real change: re-sending an unchanged weight would
        // rewrite the table (and roll its backup) for nothing.
        if (sp && picker.weight !== ""
            && String(picker.weight) !== _spoolWeightField(sp)) {
          await spoolMacro("ACE_SPOOL_SET",
                           {ID: picker.spool, WEIGHT: picker.weight});
        }
        return;
      }
      const sl = _pickerSlot();
      const args = {MATERIAL: picker.material || "PLA",
                    VENDOR: picker.vendor || "Generic",
                    SUBTYPE: picker.subtype || "Basic",
                    COLOR: (picker.color || "").replace("#", "")};
      // Bind target: the slot, or - from the head picker - the head itself
      // (feeder/manual; ACE_SPOOL_ADD HEAD= binds via the h<n> key).
      if (isHead) args.HEAD = picker.head;
      else { args.ACE = picker.ace; args.SLOT = picker.slot; }
      // The code comes off the INSERTED spool's tag, so this is exactly where
      // a second spool of the same article collides. Ask before sending;
      // cancel means no entry is created at all.
      const sku = await askFreeSku(sl && sl.rfid_data ? sl.rfid_data.sku : "");
      if (sku === null) return;
      if (sku) args.SKU = sku;
      // Empty stays empty: no invented default, a wrong start weight
      // mis-tracks the whole spool while looking plausible.
      if (picker.weight !== "") args.WEIGHT = picker.weight;
      await spoolMacro("ACE_SPOOL_ADD", args);
      await reloadState();
      // ACE_SPOOL_ADD bound it server-side; mirror that into the dialog so
      // savePicker sees no change and does not send a redundant assign.
      // Guarded, or the spool watcher would clear the weight we just set.
      _pickerOpening = true;
      picker.spool = (isHead
        ? ((state.spool_binding || {})[`h${picker.head}`] || "")
        : (spoolIdForSlot(picker.ace, picker.slot) || ""));
      nextTick(() => { _pickerOpening = false; });
    }
    function spoolSlotKey(sp) {
      return Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id) || "";
    }
    // Filament decidedly AT the gate - the mirror of the engine's occupied
    // test in ACE_SPOOL_ASSIGN (gate == AVAILABLE). 'unknown' (unit not
    // reporting yet) must NOT count as occupied: the engine would allow
    // the move, so the UI may not be stricter than its backstop.
    function slotOccupied(aceIdx, slotIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      return !!(sl && sl.state && sl.state !== 'empty'
                && sl.state !== 'unknown');
    }
    // The engine refuses to move a spool OUT of a physically occupied slot
    // (spool_bound_elsewhere, the 2026-08-02 green-spool guard). Mirrored
    // into the dropdown so the red message becomes unreachable from the
    // web (Dirk 2026-08-09: "einfach ausgegraut und nicht waehlbar ...
    // statt einer meldung wenn man es versucht"): while the row's spool
    // sits occupied, every OTHER target is greyed. Clearing stays
    // possible, and physically taking the roll out is what frees a move.
    // A head binding has no gate; the engine allows that move, so no lock.
    function spoolMoveLocked(sp, key) {
      const cur = spoolSlotKey(sp);
      if (!cur || key === cur) return false;
      const m = /^(\d+)_(\d+)$/.exec(cur);
      if (!m) return false;
      return slotOccupied(Number(m[1]), Number(m[2]));
    }
    // Assign straight from the row's dropdown. An empty pick clears the
    // binding (spool taken out); picking a slot that holds another spool
    // replaces it there - one spool per slot, the engine does the same.
    function spoolAssignTo(sp, key) {
      if (!key) {
        const cur = spoolSlotKey(sp);
        if (!cur) return;
        if (cur.startsWith("h")) {
          spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: Number(cur.slice(1))});
          return;
        }
        const [a, sl] = cur.split("_").map(Number);
        spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl});
        return;
      }
      if (key.startsWith("h")) {
        // Head binding (feeder/manual). Identity adoption goes through the
        // head's print_task_config - the same push the head picker saves
        // with - because a feeder head HAS no slot to override.
        const h = Number(key.slice(1));
        spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: h, ID: sp.id});
        if (sp.material) {
          const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
          const hex = String(sp.color || "ffffff").replace("#", "");
          enqueue("SET_PRINT_FILAMENT_CONFIG", {
            CONFIG_EXTRUDER:     h,
            FILAMENT_TYPE:       dq(sp.material),
            FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
            VENDOR:              dq(sp.vendor || "Generic"),
            FILAMENT_SUBTYPE:    dq(sp.subtype || ""),
          });
        }
        return;
      }
      const [a, sl] = key.split("_").map(Number);
      spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl, ID: sp.id});
      // ... and adopt the spool's identity for that slot, same as the
      // picker does - otherwise the slot would still show whatever was
      // declared before.
      if (sp.material) {
        fetch(`${API}/slot-override`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            ace: a, slot: sl,
            material: sp.material || "",
            color: "#" + String(sp.color || "000000").replace("#", ""),
            vendor: sp.vendor || "",
            subtype: sp.subtype || "",
          }),
        }).then(() => reloadState()).catch(() => {});
      }
    }
    function spoolUnassign(sp) {
      spoolAssignTo(sp, "");
    }
    function spoolDelete(sp) {
      confirm({
        title: t('ui.spools.delete_title'),
        message: t('ui.spools.delete_msg', {name: spoolTitle(sp)}),
        okLabel: t('ui.common.delete'),
        onOk: () => spoolMacro("ACE_SPOOL_DELETE", {ID: sp.id, FORCE: 1}),
      });
    }
    function spoolExport() {
      window.open(`${API}/spools/export`, "_blank");
    }
    async function spoolImport(fileList) {
      const f = fileList && fileList[0];
      if (spoolFileInput.value) spoolFileInput.value.value = "";
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch(`${API}/spools/import?mode=${spoolImportMode.value}`,
                              {method: "POST", body: fd});
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
      } catch (e) {
        confirm({title: t('ui.spools.title'), message: String(e),
                 dismissOnly: true, okLabel: "OK", onOk: () => {}});
      }
      reloadState();
    }
    async function setQuadReplenish(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_QUAD_REPLENISH",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Order vs stock's head-twin replenish: off = stock switches heads
    // first (instant), on = the printing head drains its own lane first
    // (reaches every spool, one reload pause each). ENABLE is re-sent
    // unchanged - the command takes both in one call.
    // The CHECKBOX is worded as the deviation ("switch heads first"), the
    // engine flag as quad_first - so the template inverts, not this. Keeping
    // the stored name means no config/save-variable migration: an [ace]
    // quad_first line that is no longer read would halt Klipper.
    async function setQuadFirst(first) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_QUAD_REPLENISH",
                                args: {ENABLE: state.quad_replenish ? 1 : 0,
                                       FIRST: first ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Per-pair purge stamps of the preflight: off = the engine ignores the
    // LENGTH stamps and purges the fixed length again, also on files that
    // already carry them. Write-through setter - live, no Save/restart.
    async function setPurgeMatrix(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PURGE",
                                args: {MATRIX: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    async function setHeadAce(idx, ace) {
      await headSet("head-ace", {head: idx, ace: Number(ace)},
                    t("ui.dashboard.head_ace"));
    }
    // head mode: connected ACEs as {value,label} for the per-head ACE dropdown.
    const aceOptions = computed(() =>
      (state.aces || []).map(a => ({
        value: a.idx,
        label: "ACE " + dispIdx(a.idx) + (a.protocol ? " (" + a.protocol.toUpperCase() + ")" : ""),
      })));
    // head mode: the ACE currently wired to a head (head_ace), defaulting to the
    // head index.
    // Tooltip of the "(V2)" marker on an ACE card: model and firmware
    // version, whichever the unit reported. Keeping the version out of the
    // visible text is what gives the header room again (Dirk 2026-08-11) -
    // 'Unknown' is the handshake's placeholder and is not worth showing.
    function aceProtoTitle(ace) {
      return [ace.model,
              (ace.firmware && ace.firmware !== "Unknown") ? ace.firmware : ""]
        .filter(Boolean).join(" · ");
    }
    function headAceOf(idx) {
      const ha = state.head_ace || {};
      const a = ha[idx] ?? ha[String(idx)];
      return (a === undefined || a === null) ? idx : Number(a);
    }
    // head mode: ACE options for one head's dropdown - exclude ACEs already
    // wired to ANOTHER ACE head (one ACE feeds exactly one head), but always
    // keep this head's own current selection.
    function aceOptionsForHead(idx) {
      const taken = new Set();
      for (const h of (state.ace_heads || [])) {
        if (Number(h) === Number(idx)) continue;
        taken.add(headAceOf(h));
      }
      const mine = headAceOf(idx);
      return aceOptions.value.filter(o => o.value === mine || !taken.has(o.value));
    }
    // head mode: true when every wired ACE head is a right-side head (internal
    // index >= 2, display 3/4) -> right-align the ACE grid so the cards start
    // from the right, lining up with the right toolheads.
    const aceHeadsRightSide = computed(() => {
      const h = state.ace_heads || [];
      return h.length > 0 && h.every(x => Number(x) >= 2);
    });
    // The ACE cards to render. In head mode: only ACEs wired to an ACE head
    // (unused ones hidden), ordered by their head index so each ACE card lines
    // up with its toolhead (T0..T3 left to right). Other modes: all ACEs as-is.
    const visibleAces = computed(() => {
      const aces = state.aces || [];
      if (state.mode !== "head") return aces;
      const byIdx = {};
      for (const a of aces) byIdx[a.idx] = a;
      const out = [];
      const seen = new Set();
      for (const h of [...(state.ace_heads || [])].sort((a, b) => a - b)) {
        const ai = headAceOf(h);
        if (byIdx[ai] && !seen.has(ai)) { out.push(byIdx[ai]); seen.add(ai); }
      }
      return out;
    });
    function loadSlot(aceIdx, slotIdx) {
      if (_blockIfPrinting()) return;
      // Before anything is enqueued: an empty source slot cannot load, and
      // the unload half would otherwise run and strand the head.
      if (slotIsEmpty(aceIdx, slotIdx)) {
        setMacroLog(t("ui.dashboard.slot_empty_hint",
                      {ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)}));
        return;
      }
      if (state.mode === "head") {
        // head mode: each ACE head is wired to exactly one ACE (head_ace), so
        // this ACE's slots all feed the head whose head_ace points here. Loading
        // a slot loads that head from this slot (swap if already loaded).
        const h = aceHeadForAce(aceIdx);
        if (h === null) return;
        // Confirm AFTER resolving the head, so the question names the head
        // that will actually move - in head mode that is not the slot index.
        if (!_confirmCmd("ui.confirm.load_slot", {head: dispIdx(h),
                ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)})) return;
        const th = state.toolheads.find(tt => tt.idx === h);
        // SAME ace/slot with the toolhead sensor CLEAR needs NO unload
        // cycle - nothing is at the head, ACE_LOAD_HEAD just (re)runs the
        // feed (its already-loaded guard reads this same eN sensor). That
        // covers BOTH: a failed load (head_source kept with
        // load_failed=true) and the mid-print runout reload (head_source
        // kept for the FA-rearm on resume; the old unconditional prefix
        // forced a pointless double unload there - Dirk 2026-07-26). A
        // different slot still unloads first (two filaments must not share
        // the path), and a head whose sensor reads filament (no-flow
        // class / really loaded) still unloads first too.
        const directLoad = !!(th && !th.filament_at_extruder
          && th.ace === aceIdx && th.slot === slotIdx);
        if (th && th.head_source_known && !directLoad) {
          enqueue("ACE_UNLOAD_HEAD", {HEAD: h});
        }
        enqueue("ACE_LOAD_HEAD", {HEAD: h, ACE: aceIdx, SLOT: slotIdx});
        return;
      }
      // multi: slot N feeds head N.
      if (!_confirmCmd("ui.confirm.load_slot", {head: dispIdx(slotIdx),
              ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)})) return;
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (th && th.head_source_known && th.ace !== aceIdx) {
        enqueue("ACE_UNLOAD_HEAD", {HEAD: slotIdx});
        enqueue("ACE_LOAD_HEAD",   {HEAD: slotIdx, ACE: aceIdx});
        return;
      }
      enqueue("ACE_LOAD_HEAD", {HEAD: slotIdx, ACE: aceIdx});
    }
    // head mode: load a feeder head via its native stock side feeder (no ACE).
    function loadFeederHead(h) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.load_head", {head: dispIdx(h)})) return;
      enqueue("ACE_LOAD_HEAD", {HEAD: h});
    }
    // Same load, but for a hybrid combo head's stock feeder tap rather
    // than its ACE slot - SOURCE=FEEDER routes cmd_ACE_LOAD_HEAD to the
    // feeder path (ace.py:12395).
    function loadComboFeederHead(h) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.load_head", {head: dispIdx(h)})) return;
      enqueue("ACE_LOAD_HEAD", {HEAD: h, SOURCE: "FEEDER"});
    }
    // head mode: the ACE head wired to this ACE (head_ace reverse lookup), or
    // null if no ACE head uses it.
    function aceHeadForAce(aceIdx) {
      const heads = state.ace_heads || [];
      const ha = state.head_ace || {};
      for (const h of heads) {
        const a = Number(ha[h] ?? ha[String(h)] ?? h);
        if (a === aceIdx) return h;
      }
      return null;
    }
    // head mode: is this ACE slot the one currently loaded into its ACE head?
    // (used to disable that slot's Load button; other slots stay loadable=swap).
    function slotLoadedInHead(aceIdx, slotIdx) {
      if (state.mode !== "head") return false;
      const h = aceHeadForAce(aceIdx);
      if (h === null) return false;
      const th = state.toolheads.find(tt => tt.idx === h);
      // load_failed: the slot is NOT actually loaded (failed load keeps
      // head_source for the retry) - keep its Load button usable as the
      // one-click retry. Same for a head whose toolhead sensor reads CLEAR
      // (manual extraction: bowden off, lever, pull, cut - Dirk's no-flow
      // recovery): head_source alone is bookkeeping, not filament. Only an
      // explicit sensor False counts (None/undefined = module offline ->
      // stay conservative, keep disabled). toolheadOps keeps the button
      // disabled while a load/unload is still RUNNING on that head (the
      // sensor clears ~30s before an unload finishes - without this the
      // button re-enabled mid-op).
      return !!(th && th.head_source_known && !th.load_failed
                && (th.filament_at_extruder !== false
                    || !!toolheadOps.value[th.idx])
                && th.ace === aceIdx && th.slot === slotIdx);
    }
    // ---- per-material tip forming ([ace_tipform]) editor ---------------
    // Server truth = the cfg section; state.tipform = what Klipper RUNS.
    // The two differ until a restart -> tipformRestartPending banner.
    const tipform = reactive({
      supported: false, mode: "stock", rows: [],
      cfgMode: "stock", cfgNames: [],
      error: "", savedMsg: "",
    });
    // A section key is '<material>' or '<vendor>_<material>' (join '_', see
    // ace_tipform.table_for). Split back for the editor: the material is the
    // LAST '_'-segment (DB materials are single tokens / hyphenated, never
    // underscored - pla, petg, pla-cf), the vendor is everything before,
    // with '_' shown as spaces for readability. 'default'/'soft' and any
    // plain material have no '_' -> vendor ''.
    function tipformSplitKey(key) {
      const k = String(key || "");
      const i = k.lastIndexOf("_");
      if (i < 0) return {material: k, vendor: ""};
      return {vendor: k.slice(0, i).replace(/_/g, " "), material: k.slice(i + 1)};
    }
    // Compose the section key from a row: vendor collapsed to lower_snake,
    // '<vendor>_<material>' or '<material>'. Mirrors ace_tipform.table_for
    // so the editor's key and the engine's constructed lookup key match.
    function tipformComposeKey(material, vendor) {
      const mat = (material || "").trim().toLowerCase();
      const ven = (vendor || "").trim().toLowerCase().split(/\s+/).filter(Boolean).join("_");
      if (!mat) return "";
      return ven ? `${ven}_${mat}` : mat;
    }
    // --- Visual token builder (web-only convenience; the raw string stays
    // the source of truth, this just parses/serializes it). Step = one token
    // {type, a, b}: move -> a@b, others -> type:a. Mirrors the token grammar
    // in ace_tipform.parse_table; the backend parse_table remains the strict
    // gate on save, so a half-built table can never reach Klipper.
    const TIPFORM_STEP_TYPES = ["move", "pause", "temp", "waittemp", "fan", "unloadtemp", "loadtemp"];
    function tipformParseSteps(tableStr) {
      const steps = [];
      for (let part of String(tableStr || "").split(",")) {
        part = part.trim();
        if (!part) continue;
        const low = part.toLowerCase();
        let m;
        if ((m = /^(pause|waittemp|unloadtemp|loadtemp|temp|fan)\s*:\s*(.*)$/.exec(low))) {
          steps.push({type: m[1], a: m[2].trim(), b: ""});
        } else if (part.includes("@")) {
          const [mm, f] = part.split("@");
          steps.push({type: "move", a: mm.trim(), b: (f || "").trim()});
        } else {
          steps.push({type: "move", a: part, b: ""});
        }
      }
      return steps;
    }
    function tipformStepToToken(s) {
      return s.type === "move" ? `${s.a}@${s.b}` : `${s.type}:${s.a}`;
    }
    function tipformStepsToTable(row) {
      row.table = (row._steps || []).map(tipformStepToToken).join(", ");
    }
    function tipformToggleBuilder(row) {
      row._builderOpen = !row._builderOpen;
      if (row._builderOpen) row._steps = tipformParseSteps(row.table);
    }
    function tipformAddStep(row) {
      if (!row._steps) row._steps = [];
      row._steps.push({type: "move", a: "", b: ""});
      tipformStepsToTable(row);
    }
    function tipformRemoveStep(row, i) {
      row._steps.splice(i, 1);
      tipformStepsToTable(row);
    }
    function tipformStepPlaceholder(step) {
      if (step.type === "move") return "mm";
      if (step.type === "pause") return "ms";
      if (step.type === "fan") return "0-255";
      return "°C";
    }
    // The firmware's stock pull (CONTROL_RETRACT_ACTION), as a starting point
    // to tune from - matches the reference in the [ace_tipform] cfg comment.
    const TIPFORM_STOCK = "57@400, 3@1500, -27@2700, -5.5@40, -37.5@1500";
    function tipformInsertStock(row) {
      row.table = TIPFORM_STOCK;
      if (row._builderOpen) row._steps = tipformParseSteps(row.table);
    }
    function _tipformNewRow(material, vendor, table) {
      return {material: material || "", vendor: vendor || "", table: table || "",
              _builderOpen: false, _steps: []};
    }
    async function loadTipform() {
      try {
        const r = await fetch(`${API}/tipform`);
        if (!r.ok) { tipform.supported = false; return; }
        const j = await r.json();
        tipform.supported = !!j.supported;
        tipform.mode = j.mode || "stock";
        tipform.rows = Object.entries(j.tables || {})
          .map(([name, table]) => {
            const s = tipformSplitKey(name);
            return _tipformNewRow(s.material, s.vendor, table);
          });
        tipform.cfgMode = tipform.mode;
        tipform.cfgNames = Object.keys(j.tables || {});
        tipform.error = "";
      } catch (_) { tipform.supported = false; }
    }
    function tipformAddRow() { tipform.rows.push(_tipformNewRow()); }
    function tipformRemoveRow(i) { tipform.rows.splice(i, 1); }
    async function saveTipform() {
      tipform.error = ""; tipform.savedMsg = "";
      const tables = {};
      for (const row of tipform.rows) {
        const tbl = (row.table || "").trim();
        const key = tipformComposeKey(row.material, row.vendor);
        if (!key && !tbl) continue;
        if (!key) { tipform.error = t("ui.config.tipform_err_name"); return; }
        tables[key] = tbl;
      }
      try {
        const resp = await fetch(`${API}/tipform`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          // Never restarts Klipper - the "restart pending" line tells the
        // user what is still missing, and Fluidd can do the restart
        // (Dirk 2026-08-09, same policy as the config save).
        body: JSON.stringify({mode: tipform.mode, tables,
                                restart_klipper: false}),
        });
        const j = await resp.json();
        if (!resp.ok || j.detail) {
          tipform.error = String(j.detail || `HTTP ${resp.status}`);
          return;
        }
        // Same feedback shape as the config save above - Dirk confused
        // the two identical buttons because this one answered with a
        // bare grey line while the other shows path+backup.
        tipform.savedMsg = `✓ ${j.path || ""}\nBackup: ${j.backup || ""}\n`
          + (j.reloaded ? t("ui.config.tipform_applied")
                        : t("ui.config.tipform_saved"));
        await loadTipform();
      } catch (e) { tipform.error = String(e); }
    }
    // Name options for a table row: the special keys + the firmware
    // filament DB (same list the slot picker offers, /api/materials),
    // lowercased to the lookup key. A row whose saved name is not in the
    // list (hand-edited cfg) keeps its own entry so the select never
    // blanks an existing table.
    function tipformNameOptions(row) {
      const opts = ['default', 'soft'];
      for (const m of pickerMaterials.value) {
        const k = String(m).toLowerCase();
        if (!opts.includes(k)) opts.push(k);
      }
      const own = (row && row.material || '').trim().toLowerCase();
      if (own && !opts.includes(own)) opts.push(own);
      return opts;
    }

    // saved cfg vs live Klipper state (NOT the unsaved editor rows)
    const tipformRestartPending = computed(() => {
      if (!tipform.supported) return false;
      const live = state.tipform || {};
      const liveMode = live.available ? (live.mode || "stock") : "stock";
      if (liveMode !== (tipform.cfgMode || "stock")) return true;
      if (liveMode === "stock") return false;
      const a = [...(live.tables || [])].map(String).sort().join(",");
      const b = [...(tipform.cfgNames || [])].sort().join(",");
      return a !== b;
    });

    // Default/fallback list; the live list + per-type subtypes are loaded
    // from /api/materials, which sources them from the firmware filament DB
    // (filament_parameters.py) - same materials the printer's display offers.
    const pickerMaterials = ref([
      "PLA", "PLA-CF",
      "PETG", "PETG-CF", "PETG-HF",
      "ABS", "ASA",
      "TPU",
      "PA", "PA-CF", "PA-GF", "PA6-CF", "PA6-GF",
      "PC", "PC-ABS",
      "PVA",
    ]);
    // Full { type: { vendor: [subtype, ...] } } hierarchy from the firmware DB.
    const pickerDb = ref({});
    async function loadMaterials() {
      try {
        const r = await fetch(`${API}/materials`);
        if (r.ok) {
          const j = await r.json();
          if (Array.isArray(j.materials) && j.materials.length) {
            pickerMaterials.value = j.materials;
          }
          if (j.db && typeof j.db === "object") {
            pickerDb.value = j.db;
          }
        }
      } catch (_) {}
    }
    // Vendors declared in [ace_tipform] keys ('<vendor>_<material>'), per
    // material - offered in the filament picker so the declared identity can
    // hit a vendor table/unloadtemp exactly (table_for matching is
    // case-insensitive, so the title-cased display value matches). Sources:
    // the LIVE Klipper tables plus the editor rows (saved or in-progress).
    function _titleCase(s) {
      return String(s || "").split(/\s+/).filter(Boolean)
        .map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
    }
    const tipformVendorsByMaterial = computed(() => {
      const map = {};
      const add = (material, vendor) => {
        const m = String(material || "").trim().toUpperCase();
        const v = _titleCase(vendor);
        if (!m || !v) return;
        if (!(map[m] = map[m] || []).includes(v)) map[m].push(v);
      };
      for (const k of (state.tipform && state.tipform.tables) || []) {
        const s = tipformSplitKey(String(k).toLowerCase());
        if (s.vendor) add(s.material, s.vendor);
      }
      for (const row of (tipform.rows || [])) {
        if (row && row.vendor) add(row.material, row.vendor);
      }
      return map;
    });
    // Vendors for the chosen material (Generic always first) straight from the
    // firmware DB, PLUS the [ace_tipform] vendors for that material (dedup,
    // case-insensitive). Tipform vendors are in the VALIDATION list on
    // purpose: the material-change cascade must not snap them away when the
    // same vendor is declared for the new material too.
    const pickerDbVendors = computed(() => {
      const v = Object.keys(pickerDb.value[picker.material] || { Generic: [] });
      const base = v.includes("Generic")
        ? ["Generic", ...v.filter(x => x !== "Generic")] : [...v];
      const low = new Set(base.map(x => x.toLowerCase()));
      const mat = String(picker.material || "").toUpperCase();
      for (const tv of tipformVendorsByMaterial.value[mat] || []) {
        if (!low.has(tv.toLowerCase())) { base.push(tv); low.add(tv.toLowerCase()); }
      }
      return base;
    });
    // Display list for the <select>: the DB vendors PLUS the slot's current
    // vendor when the printer doesn't ship it (e.g. an RFID-set brand). Without
    // this the <select> shows blank for an unknown vendor, so it can't be
    // changed or cleared. Display-only - the cascade still validates against the
    // DB list, so a user material change resets a non-DB vendor.
    const pickerVendors = computed(() => {
      const v = pickerDbVendors.value;
      return (picker.vendor && !v.includes(picker.vendor)) ? [...v, picker.vendor] : v;
    });
    // Subtypes for the chosen material + vendor from the firmware DB; "Basic" =
    // firmware 'generic'.
    const pickerDbSubtypes = computed(() => {
      const byVendor = pickerDb.value[picker.material] || {};
      return ["Basic", ...(byVendor[picker.vendor] || [])];
    });
    // Display list: DB subtypes PLUS the slot's current subtype when the printer
    // doesn't know it (e.g. an RFID-set subtype) - same display-only rationale
    // as pickerVendors (this is the RFID 'Transparent' that couldn't be cleared).
    const currentSubtypes = computed(() => {
      const s = pickerDbSubtypes.value;
      return (picker.subtype && !s.includes(picker.subtype)) ? [...s, picker.subtype] : s;
    });
    const picker = reactive({
      show: false,
      ace: 0,
      slot: 0,
      spool: "",      // bound spool id of this slot ("" = none)
      // Grams field of the spool row. Reads as "the weight of the spool in
      // this slot": with a spool picked it carries THAT spool's weight and
      // the button corrects it, without one it seeds the spool the button
      // creates. Prefilled on open and whenever the pick changes, so a
      // correction always starts from the current value instead of blank.
      weight: "",
      head: null,     // head mode: set when editing a feeder head (no ACE slot)
      material: "PLA",
      subtype: "Basic",
      vendor: "Generic",
      color: "#ffffff",
    });
    // Suppress the cascade snap while openPicker is programmatically setting the
    // fields, so an RFID-set vendor/subtype the printer doesn't know is NOT
    // snapped away on open (it's preserved + shown via the augmented lists). A
    // real user material/vendor change (flag clear) still snaps to a DB-valid
    // value. Validate against the DB lists, not the augmented display lists.
    let _pickerOpening = false;
    watch(() => picker.material, () => {
      if (_pickerOpening) return;
      if (!pickerDbVendors.value.includes(picker.vendor)) {
        picker.vendor = pickerDbVendors.value[0] || "Generic";
      }
      if (!pickerDbSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    watch(() => picker.vendor, () => {
      if (_pickerOpening) return;
      if (!pickerDbSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    // Choosing a spool ADOPTS its identity - the spool knows what it is, so
    // the user should not retype colour/material/vendor/subtype (Dirk). Set
    // under the _pickerOpening guard: the material/vendor cascade would
    // otherwise snap away a vendor or subtype the firmware DB does not list
    // (exactly the case for a third-party spool).
    watch(() => picker.spool, (id, prev) => {
      if (_pickerOpening || id === prev) return;
      // Cleared the pick -> the field is a seed for a NEW spool again, and a
      // leftover weight of the spool just released must not become its
      // start weight.
      if (!id) { picker.weight = ""; return; }
      const sp = (state.spools || {})[id];
      if (!sp) return;
      picker.weight = _spoolWeightField(sp);
      _pickerOpening = true;
      if (sp.material) picker.material = sp.material;
      if (sp.vendor) picker.vendor = sp.vendor;
      if (sp.subtype) picker.subtype = sp.subtype;
      if (sp.color) picker.color = "#" + String(sp.color).replace("#", "");
      nextTick(() => { _pickerOpening = false; });
    });
    function openPicker(ace, slot) {
      _pickerOpening = true;
      picker.head = null;
      picker.ace = ace.idx;
      picker.slot = slot.idx;
      picker.material = (slot.material || "PLA");
      picker.subtype = slot.subtype || "Basic";
      picker.vendor = slot.brand || "Generic";
      picker.color = slot.color || "#ffffff";
      // Which spool of the table sits in this slot ("" = none). Applied
      // together with the identity in savePicker.
      picker.spool = spoolIdForSlot(ace.idx, slot.idx) || "";
      picker.weight = _spoolWeightField((state.spools || {})[picker.spool]);
      picker.show = true;
      // Let the watchers' snap run again only after this open settles.
      nextTick(() => { _pickerOpening = false; });
    }
    // head mode: edit a feeder head's filament identity (color/material). It has
    // no ACE slot - the values go straight to the head's print_task_config via
    // SET_PRINT_FILAMENT_CONFIG (same path the touchscreen uses; the heartbeat
    // leaves feeder heads untouched). The RFID/load-after buttons hide because
    // _pickerSlot() is null without an ACE slot.
    function openHeadPicker(th) {
      _pickerOpening = true;
      headRfidNote.value = "";
      picker.ace = null;
      picker.slot = null;
      picker.head = th.idx;
      picker.material = (th.material || "PLA");
      picker.subtype = th.subtype || "Basic";
      picker.vendor = th.brand || "Generic";
      picker.color = th.color || "#ffffff";
      // Head binding ('h<n>') - the same init the slot picker does, so the
      // weight field and the generic picker.spool watcher work unchanged.
      picker.spool = (state.spool_binding || {})[`h${th.idx}`] || "";
      picker.weight = _spoolWeightField((state.spools || {})[picker.spool]);
      picker.show = true;
      nextTick(() => { _pickerOpening = false; });
    }
    function closePicker() {
      picker.show = false;
      // The search dropdown must not survive the dialog: reopened later it
      // would show stale rows over a different slot/head context.
      smOpen.value = false;
      smQuery.value = "";
      smRows.value = [];
    }
    function _pickerSlot() {
      const a = state.aces.find(x => x.idx === picker.ace);
      if (!a) return null;
      return (a.slots || []).find(s => s.idx === picker.slot) || null;
    }
    const pickerHasRfid = computed(() => {
      if (!picker.show) return false;
      const s = _pickerSlot();
      return !!(s && s.rfid === 2 && s.rfid_data);
    });
    // The SKU written on the tag, shown next to the RFID button. Comes from
    // rfid_data, which the backend only fills on a LIVE read (rfid == 2), so
    // it can never be a stale value from an earlier spool.
    const pickerRfidSku = computed(() => {
      if (!picker.show) return "";
      const s = _pickerSlot();
      const v = s && s.rfid_data ? s.rfid_data.sku : "";
      return v ? String(v).trim() : "";
    });
    // Head picker twin of pickerRfidSku: the last code the STOCK feeder
    // reader delivered for this head (card UID hex, else the M1 layout's
    // numeric SKU) - state.head_tag_seen, filled by the Klipper capture.
    const pickerHeadTag = computed(() => {
      if (!picker.show) return "";
      if (picker.head === null || picker.head === undefined) return "";
      return String((state.head_tag_seen || {})[String(picker.head)] || "");
    });
    const headRfidBusy = ref(false);
    const headRfidNote = ref("");
    async function readHeadRfid() {
      // On-demand feeder-reader read: FILAMENT_DT_UPDATE CHANNEL=<head> is
      // the stock command behind the insert-event read. The result arrives
      // asynchronously (~0.7-1.5s start-to-parse), so wait, refresh, then
      // report honestly - a silent button was the §41 class.
      if (picker.head === null || picker.head === undefined) return;
      if (headRfidBusy.value) return;
      headRfidBusy.value = true;
      headRfidNote.value = "";
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "FILAMENT_DT_UPDATE",
                                args: {CHANNEL: picker.head}}),
        });
        await new Promise(r => setTimeout(r, 2500));
        await reloadState();
        const tag = (state.head_tag_seen || {})[String(picker.head)] || "";
        if (!tag) {
          headRfidNote.value = t("ui.dialog.head_no_tag");
        } else if (state.spool_mode === "spoolman") {
          // A freshly registered card_uid should adopt NOW, not on the
          // next sweep tick (the Klipper-side capture dedupes an
          // unchanged code, so the kick is the immediate path).
          try {
            await fetch(`${API}/spoolman/adopt_by_tags`, {method: "POST"});
          } catch (e) { /* sweep tick covers it */ }
          await reloadState();
        }
      } catch (e) {
        headRfidNote.value = String(e);
      } finally {
        headRfidBusy.value = false;
      }
    }
    const pickerRfidStyle = computed(() => {
      if (!pickerHasRfid.value) return {};
      const c = (_pickerSlot()?.rfid_data?.color || "").trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(c)) return {};
      const r = parseInt(c.slice(1, 3), 16);
      const g = parseInt(c.slice(3, 5), 16);
      const b = parseInt(c.slice(5, 7), 16);
      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      return {
        background: c,
        borderColor: c,
        color: lum > 0.55 ? "#001619" : "#ffffff",
      };
    });
    // Slots without an RFID tag get a "Clear" button instead of "Read
    // RFID"; shown only when the slot's identity actually comes from a
    // manual override (source === "override"), so RFID/derived/empty
    // slots offer nothing to clear.
    const pickerHasOverride = computed(() => {
      if (!picker.show) return false;
      const s = _pickerSlot();
      return !!(s && s.source === "override");
    });
    function readPickerRfid() {
      const s = _pickerSlot();
      const r = s && s.rfid_data;
      if (!r) return;
      // Reset the WHOLE identity to the tag's values unconditionally. Guarding
      // each field on a truthy RFID value left a manually-changed vendor/subtype
      // stuck when the tag had none (empty brand/subtype) - then it never
      // matched _pickerMatchesRfid and save kept a shadow override. Empty tag
      // fields fall back to the same Generic/Basic placeholders openPicker uses,
      // so save then matches the tag and drops the override. _pickerOpening
      // suppresses the cascade snap while we set the fields (as in openPicker),
      // so a tag vendor/subtype the printer doesn't ship isn't snapped away.
      _pickerOpening = true;
      picker.material = r.material || "PLA";
      picker.subtype  = r.subtype  || "Basic";
      picker.vendor   = r.brand    || "Generic";
      picker.color    = r.color    || "#ffffff";
      nextTick(() => { _pickerOpening = false; });
    }
    function _ptcGcodeFor(aceIdx, slotIdx, mat, brand, sub, colorHex) {
      const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
      const hex = (colorHex || "#ffffff").replace("#", "");
      const colorRGBA = hex.toUpperCase() + "FF";
      return {
        CONFIG_EXTRUDER: slotIdx,
        FILAMENT_TYPE:   dq(mat || "PLA"),
        FILAMENT_COLOR_RGBA: colorRGBA,
        VENDOR:          dq(brand || "Generic"),
        FILAMENT_SUBTYPE: dq(sub || ""),
      };
    }
    // Bug 1: saving an RFID slot unchanged must not create an override
    // that masks the tag. openPicker prefills Generic/Basic placeholders
    // for empty vendor/subtype, so normalise those to "" when comparing
    // the form against the tag's rfid_data.
    function _ovNorm(s) { return String(s || "").trim().toLowerCase(); }
    function _ovVendor(s) { const v = _ovNorm(s); return v === "generic" ? "" : v; }
    function _ovSub(s) { const v = _ovNorm(s); return (v === "basic" || v === "generic") ? "" : v; }
    function _ovColor(s) { return _ovNorm(s).replace(/^#/, ""); }
    // Save can carry the edit into the SPOOL, not just the slot - otherwise a
    // correction is lost the moment the picker re-reads the spool's values.
    //
    // WHAT may be written back mirrors the enrichment exactly: only fields
    // the TABLE is authoritative for.
    //   sub-type - the tag has no field for it at all
    //   weight   - likewise never on a tag
    //   vendor   - the tag HAS a brand field, so the table only owns it while
    //              that field is empty (which is what writer apps leave)
    //   material, colour - the TAG owns these; changing them permanently is a
    //              tag job, so an RFID slot does not offer them here.
    // Without a tag the table is authoritative for everything.
    //
    // Asks before changing the IDENTITY - that record is read by other slots
    // and future prints, so it must not change as a side effect of saving a
    // slot. The WEIGHT is exempt: the field only exists while a spool is
    // picked and can only ever land on that spool, so there is nothing to
    // ask about (Dirk). Declining the identity question still saves it.
    async function _spoolWriteBackFromPicker() {
      const id = picker.spool;
      const sp = id ? (state.spools || {})[id] : null;
      if (!sp) return;
      const slot = _pickerSlot();
      const rfid = (slot && slot.rfid === 2) ? slot.rfid_data : null;
      const args = {}, names = [];
      const silent = {};
      const add = (key, label, val) => { args[key] = val; names.push(t(label)); };
      if (!rfid) {
        if (_ovNorm(picker.material) !== _ovNorm(sp.material) && picker.material)
          add("MATERIAL", "ui.spools.material", picker.material);
        if (_ovColor(picker.color) !== _ovColor(sp.color) && picker.color)
          add("COLOR", "ui.spools.color", (picker.color || "").replace("#", ""));
      }
      if ((!rfid || !_ovVendor(rfid.brand))
          && _ovVendor(picker.vendor) !== _ovVendor(sp.vendor))
        add("VENDOR", "ui.spools.vendor", picker.vendor || "");
      if (_ovSub(picker.subtype) !== _ovSub(sp.subtype))
        add("SUBTYPE", "ui.spools.subtype", picker.subtype || "");
      if (picker.weight !== "" && String(picker.weight) !== _spoolWeightField(sp))
        silent.WEIGHT = picker.weight;
      const ask = names.length
        && window.confirm(t("ui.spools.write_back", {
             spool: spoolTitle(sp), fields: names.join(", ")}));
      const payload = Object.assign({}, silent, ask ? args : {});
      if (!Object.keys(payload).length) return;
      await spoolMacro("ACE_SPOOL_SET", Object.assign({ID: id}, payload));
    }
    function _pickerMatchesRfid() {
      const s = _pickerSlot();
      const r = (s && s.rfid === 2) ? s.rfid_data : null;
      if (!r) return false;
      return _ovNorm(picker.material) === _ovNorm(r.material)
          && _ovVendor(picker.vendor) === _ovVendor(r.brand)
          && _ovSub(picker.subtype) === _ovSub(r.subtype)
          && _ovColor(picker.color) === _ovColor(r.color);
    }
    async function savePicker(loadAfter) {
      // "Save + load" is a load like any other; plain "Save" only writes the
      // identity and is not gated.
      if (loadAfter && picker.ace !== null && picker.ace !== undefined
          && !_confirmCmd("ui.confirm.load_slot", {
                head: dispIdx(state.mode === "head"
                                ? (aceHeadForAce(picker.ace) ?? picker.slot)
                                : picker.slot),
                ace: dispIdx(picker.ace), slot: dispIdx(picker.slot)})) return;
      // Feeder head (no ACE slot): push the identity straight to the head's
      // print_task_config via SET_PRINT_FILAMENT_CONFIG (same path the
      // touchscreen uses). The heartbeat leaves feeder/manual heads untouched,
      // so this sticks until the user changes it.
      if (picker.head !== null && picker.head !== undefined) {
        // Spool binding travels with the identity here too - the head
        // picker was the one place the binding was NOT reachable from
        // (Dirk 2026-08-09: "fehlt die ganze spool bindung??"); the rows
        // were slot-gated. Only sent on a real change, like the slot flow.
        const hb = (state.spool_binding || {})[`h${picker.head}`] || "";
        if ((picker.spool || "") !== hb) {
          if (picker.spool) {
            enqueue("ACE_SPOOL_ASSIGN", {HEAD: picker.head, ID: picker.spool});
          } else {
            enqueue("ACE_SPOOL_ASSIGN", {HEAD: picker.head});
          }
        }
        if (picker.spool && picker.weight !== "") {
          const sp = (state.spools || {})[picker.spool];
          if (sp && String(picker.weight) !== _spoolWeightField(sp)) {
            enqueue("ACE_SPOOL_SET", {ID: picker.spool, WEIGHT: picker.weight});
          }
        }
        const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
        const hex = (picker.color || "#ffffff").replace("#", "");
        enqueue("SET_PRINT_FILAMENT_CONFIG", {
          CONFIG_EXTRUDER:     picker.head,
          FILAMENT_TYPE:       dq(picker.material || "PLA"),
          FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
          VENDOR:              dq(picker.vendor || "Generic"),
          FILAMENT_SUBTYPE:    dq(picker.subtype || ""),
        });
        closePicker();
        reloadState();
        return;
      }
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      // Spool binding travels with the identity: the picker IS the place
      // where the user says what sits in this slot. Only sent when it
      // changed, so a plain colour edit does not touch the table.
      const spoolBefore = spoolIdForSlot(aceIdx, slotIdx) || "";
      if ((picker.spool || "") !== spoolBefore) {
        if (picker.spool) {
          enqueue("ACE_SPOOL_ASSIGN",
                  {ACE: aceIdx, SLOT: slotIdx, ID: picker.spool});
        } else {
          enqueue("ACE_SPOOL_ASSIGN", {ACE: aceIdx, SLOT: slotIdx});
        }
      }
      await _spoolWriteBackFromPicker();
      if (_pickerMatchesRfid()) {
        // Values equal the RFID tag -> drop any existing override so the
        // RFID identity stays the source of truth (no shadow override).
        try {
          await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
        } catch (e) {
          setMacroLog(`${t("ui.common.error")}: ${e}`);
        }
        closePicker();
        enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        if (loadAfter) {
          loadSlot(aceIdx, slotIdx);
        }
        reloadState();
        return;
      }
      try {
        await fetch(`${API}/slot-override`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            ace: aceIdx,
            slot: slotIdx,
            material: picker.material || "",
            brand:    picker.vendor || "",
            subtype:  picker.subtype || "",
            color:    picker.color || "",
          }),
        });
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      if (loadAfter) {
        loadSlot(aceIdx, slotIdx);
      }
      reloadState();
    }
    async function clearPickerOverride() {
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      try {
        await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      reloadState();
    }
    let _lastActive = null;
    watch(() => state.active_device, (newAce) => {
      _lastActive = newAce;
    });
    // A SET, not one index: each ACE has its own thresholds, and comparing
    // or setting two of them meant the second card closing the first
    // (Dirk: "es ist nur das Dry Menü einer Karte sichtbar").
    const dryOpenAces = reactive(new Set());
    function dryPanelOpen(aceIdx) { return dryOpenAces.has(aceIdx); }
    function toggleDryPanel(aceIdx) {
      if (dryOpenAces.has(aceIdx)) dryOpenAces.delete(aceIdx);
      else dryOpenAces.add(aceIdx);
    }
    // Placeholder for the global feeder_retract_length Settings field: what
    // actually applies when left blank (falls back to retract_length).
    function feederRetractLengthEffective() {
      return configForm.feeder_retract_length !== ''
        ? configForm.feeder_retract_length : configForm.retract_length;
    }
    function aceDrying(ace) {
      const d = ace && ace.dryer;
      return !!(d && d.status && d.status !== 'stop');
    }
    function dryStart(aceIdx) {
      const cfg = dryerCfg[aceIdx] || {duration: 240};
      // Temperature comes from the unit's stored setting - the same one
      // auto-dry uses, and the same one the field above edits. Only the
      // duration is a per-click choice (auto-dry has no duration: it runs
      // until the humidity target is met).
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const temp = (a && a.auto_dry && a.auto_dry.temp) || 50;
      run("ACE_DRY", {ACE: aceIdx, TEMP: temp, DURATION: cfg.duration});
    }
    function dryStop(aceIdx) {
      run("ACE_STOP_DRYING", {ACE: aceIdx});
    }
    const snapshots = ref([]);
    const selectedSnapshot = ref("");
    const snapshotPreview = computed(() => snapshots.value.find(s => s.name === selectedSnapshot.value));
    // Head-mode snapshots are stored separately from multi - tag every snapshot
    // call with the current mode so each shows/saves its own set.
    function _snapMode() { return state.mode === "head" ? "head" : ""; }
    function _snapQS() { return state.mode === "head" ? "?mode=head" : ""; }
    async function reloadSnapshots() {
      try {
        const r = await fetch(`${API}/snapshots${_snapQS()}`);
        if (!r.ok) return;
        const j = await r.json();
        snapshots.value = j.snapshots || [];
      } catch (_) {}
    }
    // Reload the right snapshot set (and drop a stale selection) on mode switch.
    watch(() => state.mode, () => { selectedSnapshot.value = ""; reloadSnapshots(); });
    async function _doSaveSnapshot(name) {
      try {
        const r = await fetch(`${API}/snapshots`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, mode: _snapMode()}),
        });
        if (!r.ok) {
          setMacroLog(t("ui.log.snapshot_save_failed", {error: await r.text()}));
          return;
        }
        setMacroLog(t("ui.log.snapshot_saved", {name}));
        await reloadSnapshots();
        selectedSnapshot.value = name;
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function saveSnapshot() {
      if (selectedSnapshot.value) {
        const name = selectedSnapshot.value;
        confirm({
          title: t("ui.dialog.overwrite_snapshot_title", {name}),
          message: t("ui.dialog.overwrite_snapshot_msg", {name}),
          okLabel: t("ui.common.save"),
          onOk: () => _doSaveSnapshot(name),
        });
        return;
      }
      const name = prompt(t("ui.dashboard.snapshot_name_prompt"));
      if (!name) return;
      await _doSaveSnapshot(name);
    }
    async function deleteSnapshot() {
      if (!selectedSnapshot.value) return;
      if (!confirmSync(t("ui.dialog.delete_snapshot", {name: selectedSnapshot.value}))) return;
      try {
        await fetch(`${API}/snapshots/${encodeURIComponent(selectedSnapshot.value)}${_snapQS()}`, {method: "DELETE"});
        selectedSnapshot.value = "";
        await reloadSnapshots();
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function loadSnapshot() {
      if (!selectedSnapshot.value) return;
      const name = selectedSnapshot.value;
      let plan;
      try {
        const r = await fetch(`${API}/snapshots/${encodeURIComponent(name)}/apply${_snapQS()}`, {method: "POST"});
        plan = await r.json();
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
        return;
      }
      const errs = plan.errors || [];
      const warns = plan.warnings || [];
      const actions = plan.actions || [];
      if (errs.length) {
        confirm({
          title: t("ui.dialog.snapshot_errors_title"),
          message: errs.map(e => "• " + e.message).join("<br>"),
          okLabel: "OK",
          dismissOnly: true,
          onOk: () => {},
        });
        return;
      }
      const proposals = plan.override_proposals || [];
      const writeOverridesAndEnqueue = async (writeOverrides) => {
        if (writeOverrides && proposals.length) {
          for (const o of proposals) {
            try {
              await fetch(`${API}/slot-override`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(o),
              });
            } catch (e) {
              setMacroLog(`${t("ui.common.error")}: ${e}`);
            }
          }
          enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        }
        for (const a of actions) {
          enqueue(a.name, a.args || {});
        }
        // What the collapsed Loadouts header reports. Set only here, on
        // an apply that actually went through.
        appliedSnapshot.value = name;
        localStorage.setItem("multiace.appliedSnapshot", name);
      };
      if (warns.length) {
        confirm({
          title: t("ui.dialog.snapshot_warnings_title"),
          message: warns.map(w => "• " + w.message).join("<br>")
                   + "<br><br>" + t("ui.dialog.snapshot_warnings_hint"),
          okLabel: t("ui.dialog.apply_anyway"),
          checkboxLabel: proposals.length
            ? t("ui.dialog.set_filaments_per_snapshot")
            : null,
          checkboxDefault: false,
          onOk: ({checked}) => { writeOverridesAndEnqueue(checked); },
        });
        return;
      }
      confirm({
        title: t("ui.dialog.apply_snapshot_title", {name}),
        message: t("ui.dialog.apply_snapshot_msg"),
        okLabel: t("ui.common.apply"),
        onOk: () => { writeOverridesAndEnqueue(false); },
      });
    }
    const confirmDialog = reactive({
      show: false, title: "", message: "",
      okLabel: "OK",  _onOk:  null,
      altLabel: null, _onAlt: null,
      dismissOnly: false,
      checkboxLabel: null, checkboxChecked: false,
      // Optional single input. `validate` returns an error string that BLOCKS
      // OK (not a warning) - the one caller so far, the SKU collision, must
      // never be able to hand back a code another spool already holds.
      inputLabel: null, inputValue: "", inputHint: null, _validate: null,
    });
    const confirmInputError = computed(() =>
      confirmDialog._validate ? (confirmDialog._validate(confirmDialog.inputValue) || "") : "");
    function confirm(opts) {
      confirmDialog.show = true;
      confirmDialog.title = opts.title || t("ui.common.confirm");
      confirmDialog.message = opts.message || "";
      confirmDialog.okLabel = opts.okLabel || "OK";
      confirmDialog._onOk   = opts.onOk || (()=>{});
      confirmDialog.altLabel = opts.altLabel || null;
      confirmDialog._onAlt   = opts.onAlt || null;
      // The built-in Cancel closes the dialog; a caller that also has to tear
      // something down behind it (an open preflight modal) hooks it here
      // instead of passing a SECOND button that also says "Cancel".
      confirmDialog._onCancel = opts.onCancel || null;
      confirmDialog.dismissOnly = !!opts.dismissOnly;
      confirmDialog.checkboxLabel = opts.checkboxLabel || null;
      confirmDialog.checkboxChecked = !!opts.checkboxDefault;
      confirmDialog.inputLabel = opts.inputLabel || null;
      confirmDialog.inputValue = opts.inputValue == null ? "" : String(opts.inputValue);
      confirmDialog.inputHint = opts.inputHint || null;
      confirmDialog._validate = opts.validate || null;
    }
    function okConfirm() {
      // A blocking validation error must survive Enter as well as the button,
      // so the guard sits here and not only on :disabled.
      if (confirmInputError.value) return;
      const cb = confirmDialog._onOk;
      const checked = confirmDialog.checkboxChecked;
      const value = confirmDialog.inputValue;
      confirmDialog.show = false;
      if (cb) cb({checked, value});
    }
    function altConfirm() {
      const cb = confirmDialog._onAlt;
      confirmDialog.show = false;
      if (cb) cb();
    }
    function cancelConfirm() {
      const cb = confirmDialog._onCancel;
      confirmDialog.show = false;
      if (cb) cb();
    }
    function confirmSync(msg) { return window.confirm(msg); }
    const config = reactive({path: "", content: "", params: {},
                             sha1: ""});
    const configLog = ref("");
    const configLoadError = ref("");
    const showRawConfig = ref(false);
    const configForm = reactive({
      ace_device_count: 1,
      feed_speed: 80,
      retract_speed: 80,
      load_length: 2100,
      retract_length: 1950,
      seat_overshoot_length: '',
      swap_retract_length: '',
      swap_purge_length: '',
      feeder_load_length: '',
      feeder_retract_length: '',
      feeder_swap_retract_length: '',
      dryer_temp: '',
      dryer_duration: '',
      display_index_base: 0,
      v2_order: 'first',
      load_retry: '',
      extrusion_retry: '',
      unload_retry: '',
      filament_load_max_auto_retries: '',
      filament_load_retry_delay_ms: '',
      state_debug: false,
      usb_debug: false,
      fa_debug: false,
    });
    // True after a config save, which needs a full printer restart to take
    // effect (a bare Klipper restart misses USB/serial + PAXX boot-script
    // changes). Drives the prominent top reboot banner; cleared once a restart
    // actually starts (klippy down). Mode changes do NOT use this - crossing
    // 'normal' raises a backend reboot error that reaches the display too.
    const rebootNeeded = ref(false);
    function paramsToForm(params) {
      if (!params) return;
      const num  = (k) => params[k] != null ? Number(params[k]) : configForm[k];
      const bool = (k) => params[k] != null ? params[k] === 'true' : configForm[k];
      const numOrEmpty = (v) => (v != null && v !== '') ? Number(v) : '';
      configForm.ace_device_count = num('ace_device_count');
      configForm.feed_speed     = num('feed_speed');
      configForm.retract_speed  = num('retract_speed');
      configForm.load_length    = num('load_length');
      configForm.retract_length = num('retract_length');
      configForm.seat_overshoot_length = numOrEmpty(params.seat_overshoot_length);
      configForm.swap_retract_length = numOrEmpty(params.swap_retract_length);
      configForm.swap_purge_length = numOrEmpty(params.swap_purge_length);
      configForm.feeder_load_length = numOrEmpty(params.feeder_load_length);
      configForm.feeder_retract_length = numOrEmpty(params.feeder_retract_length);
      configForm.feeder_swap_retract_length =
        numOrEmpty(params.feeder_swap_retract_length);
      configForm.dryer_temp        = numOrEmpty(params.dryer_temp);
      configForm.dryer_duration    = numOrEmpty(params.dryer_duration);
      configForm.display_index_base = numOrEmpty(params.display_index_base);
      configForm.v2_order = (params.v2_order === 'last') ? 'last' : 'first';
      configForm.load_retry        = numOrEmpty(params.load_retry);
      configForm.extrusion_retry   = numOrEmpty(params.extrusion_retry);
      configForm.unload_retry      = numOrEmpty(params.unload_retry);
      configForm.filament_load_max_auto_retries =
        numOrEmpty(params.filament_load_max_auto_retries);
      configForm.filament_load_retry_delay_ms =
        numOrEmpty(params.filament_load_retry_delay_ms);
      configForm.state_debug    = bool('state_debug');
      configForm.usb_debug      = bool('usb_debug');
      configForm.fa_debug       = bool('fa_debug');
    }
    function formToCfgContent(content) {
      const lines = content.split('\n');
      const numStr = (v) => (v === '' || v == null) ? '' : String(v);
      const mainRepl = {
        ace_device_count:   numStr(configForm.ace_device_count),
        feed_speed:         numStr(configForm.feed_speed),
        retract_speed:      numStr(configForm.retract_speed),
        load_length:        numStr(configForm.load_length),
        retract_length:     numStr(configForm.retract_length),
        seat_overshoot_length: numStr(configForm.seat_overshoot_length),
        swap_retract_length: numStr(configForm.swap_retract_length),
        swap_purge_length:   numStr(configForm.swap_purge_length),
        feeder_load_length:  numStr(configForm.feeder_load_length),
        feeder_retract_length: numStr(configForm.feeder_retract_length),
        feeder_swap_retract_length:
                            numStr(configForm.feeder_swap_retract_length),
        dryer_temp:         numStr(configForm.dryer_temp),
        dryer_duration:     numStr(configForm.dryer_duration),
        display_index_base: numStr(configForm.display_index_base),
        v2_order:           configForm.v2_order === 'last' ? 'last' : 'first',
        load_retry:         numStr(configForm.load_retry),
        extrusion_retry:    numStr(configForm.extrusion_retry),
        unload_retry:       numStr(configForm.unload_retry),
        filament_load_max_auto_retries:
                            numStr(configForm.filament_load_max_auto_retries),
        filament_load_retry_delay_ms:
                            numStr(configForm.filament_load_retry_delay_ms),
        state_debug:        configForm.state_debug ? 'true' : 'false',
        usb_debug:          configForm.usb_debug   ? 'true' : 'false',
        fa_debug:           configForm.fa_debug    ? 'true' : 'false',
      };
      const keyRegex = /^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/;
      const sectionRegex = /^\s*\[(.+?)\]\s*$/;
      const out = [];
      let curSection = null;
      const sectionEnd = {};
      const seenInSection = {};
      const seenSet = (sec) => {
        const k = sec === 'ace' ? 'ace' : `ace${sec}`;
        if (!seenInSection[k]) seenInSection[k] = new Set();
        return seenInSection[k];
      };
      const closeSection = () => {
        if (curSection === null) return;
        const k = curSection === 'ace' ? 'ace' : `ace${curSection}`;
        sectionEnd[k] = out.length;
      };
      for (const raw of lines) {
        const sm = raw.match(sectionRegex);
        if (sm) {
          closeSection();
          const head = sm[1].trim();
          if (head === 'ace') {
            curSection = 'ace';
          } else if (head.startsWith('ace ') || head.startsWith('ace\t')) {
            const idx = parseInt(head.split(/\s+/, 2)[1], 10);
            curSection = isNaN(idx) ? null : idx;
          } else {
            curSection = null;
          }
          out.push(raw);
          continue;
        }
        if (curSection === 'ace') {
          const m = raw.match(keyRegex);
          if (m && (m[1] in mainRepl)) {
            const key = m[1];
            const val = mainRepl[key];
            seenSet('ace').add(key);
            if (val === '' || val == null) continue;
            out.push(`${key}: ${val}`);
            continue;
          }
        }
        out.push(raw);
      }
      closeSection();
      const insertMissing = (sectionLabel, repl, seen) => {
        const missing = Object.keys(repl)
          .filter(k => !seen.has(k))
          .filter(k => repl[k] !== '' && repl[k] != null);
        if (!missing.length) return;
        const sectionKey = sectionLabel === '[ace]' ? 'ace'
          : `ace${sectionLabel.match(/\[ace (\d+)\]/)[1]}`;
        const endIdx = sectionEnd[sectionKey];
        const block = missing.map(k => `${k}: ${repl[k]}`);
        if (endIdx != null) {
          out.splice(endIdx, 0, ...block);
          for (const k of Object.keys(sectionEnd)) {
            if (sectionEnd[k] > endIdx) sectionEnd[k] += block.length;
          }
        } else {
          out.push('', sectionLabel, ...block);
        }
      };
      insertMissing('[ace]', mainRepl, seenSet('ace'));
      return out.join('\n');
    }
    const updateState = reactive({
      current: "",
      latest: "",
      statusText: "",
      canApply: false,
      busy: null,
      log: "",
    });
    const debugState = reactive({
      enabled: false,
      busy: false,
      rebootPrompt: false,
    });
    async function refreshDebugState() {
      try {
        const r = await fetch(`${API}/debug-mode`);
        const j = await r.json();
        if (r.ok) debugState.enabled = !!j.enabled;
      } catch (e) {
      }
    }
    async function debugEnable() {
      if (debugState.busy) return;
      debugState.busy = true;
      try {
        const r = await fetch(`${API}/debug-mode/enable`, {method: "POST"});
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        debugState.enabled = !!j.enabled;
        debugState.rebootPrompt = debugState.enabled;
      } catch (e) {
        setMacroLog(`${t("ui.config.debug_enable_failed")}: ${e.message || e}`);
      } finally {
        debugState.busy = false;
      }
    }
    async function debugDisable() {
      if (debugState.busy) return;
      confirm({
        title: t("ui.config.debug_disable_title"),
        message: t("ui.config.debug_disable_msg"),
        okLabel: t("ui.config.debug_disable_btn"),
        onOk: async () => {
          debugState.busy = true;
          try {
            const r = await fetch(`${API}/debug-mode/disable`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            debugState.enabled = !!j.enabled;
            debugState.rebootPrompt = false;
          } catch (e) {
            setMacroLog(`${t("ui.config.debug_disable_failed")}: ${e.message || e}`);
          } finally {
            debugState.busy = false;
          }
        },
      });
    }
    // The reboot step debugEnable's success prompt asks for. Reuses the
    // backend's existing /api/reboot (Moonraker's /machine/reboot) - there
    // was no other caller of it in the whole frontend, which is exactly the
    // kind of gap that leaves a working backend feature with no way to
    // trigger it from the UI.
    async function debugReboot() {
      if (debugState.busy) return;
      confirm({
        title: t("ui.config.debug_reboot_title"),
        message: t("ui.config.debug_reboot_msg"),
        okLabel: t("ui.config.debug_reboot_btn"),
        onOk: async () => {
          debugState.busy = true;
          try {
            const r = await fetch(`${API}/reboot`, {method: "POST"});
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            debugState.rebootPrompt = false;
            setMacroLog(t("ui.config.debug_reboot_sent"));
          } catch (e) {
            setMacroLog(`${t("ui.config.debug_reboot_failed")}: ${e.message || e}`);
          } finally {
            debugState.busy = false;
          }
        },
      });
    }
    function _parseUpdateResult(r) {
      const lines = r.status_lines || [];
      let cur = updateState.current, lat = updateState.latest;
      let canApply = false, statusText = "";
      for (const line of lines) {
        const mCur = line.match(/current=(\S+)/);
        if (mCur) cur = mCur[1];
        const mLat = line.match(/latest=(\S+)/);
        if (mLat) lat = mLat[1];
        const mTo = line.match(/to=(\S+)/);
        if (mTo) lat = mTo[1];
        if (line.startsWith("update_available")) canApply = true;
        if (line.startsWith("up_to_date") || line.startsWith("done")
            || line.startsWith("refusing_downgrade")) canApply = false;
        statusText = line;
      }
      updateState.current = cur || updateState.current;
      updateState.latest = lat || updateState.latest;
      updateState.canApply = canApply;
      updateState.statusText = statusText;
      updateState.log = r.stdout || "";
    }
    async function updateCheck() {
      if (updateState.busy) return;
      updateState.busy = "check";
      try {
        const r = await fetch(`${API}/update/check`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        _parseUpdateResult(j);
      } catch (e) {
        updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
        setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
      } finally {
        updateState.busy = "";
      }
    }
    async function updateApply() {
      if (updateState.busy) return;
      confirm({
        title: t("ui.config.update_apply_title"),
        message: t("ui.config.update_apply_msg", {
          from: updateState.current || "?",
          to:   updateState.latest  || "latest",
        }),
        okLabel: t("ui.config.update_apply_btn"),
        onOk: async () => {
          updateState.busy = "apply";
          try {
            const r = await fetch(`${API}/update/apply`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            _parseUpdateResult(j);
            if (j.ok) {
              setMacroLog(t("ui.config.update_done"));
            }
          } catch (e) {
            updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
            setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
          } finally {
            updateState.busy = "";
          }
        },
      });
    }
    async function loadConfig() {
      configLoadError.value = "";
      try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        config.path = j.path || "";
        config.content = j.content || "";
        config.params = j.params || {};
        // Revision token for the lost-update guard (see saveConfigForm).
        config.sha1 = j.sha1 || "";
        paramsToForm(j.params);
      } catch (e) {
        configLoadError.value = t("ui.log.config_load_failed", {error: e});
      }
    }
    // LOST-UPDATE GUARD (HW 2026-07-30): the form PATCHES the browser's
    // cached copy of the file, so a tab that loaded an older revision
    // silently writes it back - a cfg repaired via SSH was reverted to a
    // section-less version, losing SET_ACE_MODE, [ace_bg_swap] and
    // [ace_tipform]. We send the sha1 we loaded; on 409 the server
    // returns the CURRENT content and we re-apply the form values on top
    // of it and retry once, so the user's edit lands without clobbering
    // whatever else changed on disk meanwhile. See _putConfig() and
    // saveConfigForm() further down, next to the apply-changes modal.
    async function saveConfigRaw() {
      configLog.value = t("ui.log.saving_raw");
      try {
        // Same guard as the form save, but NO auto-retry: the raw editor's
        // content IS the user's text - silently rebasing it would discard
        // either their edit or the on-disk change. Report the conflict and
        // let them reload.
        const r = await fetch(`${API}/config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          // Never restart Klipper from here - same policy as the form save
          // (Dirk 2026-08-09: "mal klappt speichern mit klipper restart, mal
          // nicht, einfach printer restart verlangen"). A bare Klipper
          // restart applies most [ace] scalars but misses USB/serial
          // re-enumeration and PAXX boot scripts, so it produced a
          // half-applied config and once a 503 mid-restart.
          body: JSON.stringify({content: config.content,
                                restart_klipper: false,
                                base_sha1: config.sha1}),
        });
        if (r.status === 409) {
          configLog.value = t("ui.log.config_conflict_raw");
          return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        const j = await r.json();
        config.sha1 = j.sha1 || "";
        rebootNeeded.value = true;
        configLog.value = `✓ ${j.path}\nBackup: ${j.backup}\n${t("ui.common.please_restart")}`;
      } catch (e) { configLog.value = `${t("ui.common.error")}: ${e}`; }
    }
    async function setMode(m) {
      // multi<->head is a runtime flip (no reboot); only transitions crossing
      // 'normal' need a Klipper restart. In head mode each head is toggled to
      // feeder individually (per-head feeder checkbox), no single ACE head.
      if (state.mode === m) return;
      // Mode changes that cross 'normal' (stock<->ACE file swap) require all
      // toolheads unloaded - mirror the SET_ACE_MODE macro guard client-side so
      // the "unload first" rejection is visible in the web, not only in Fluidd's
      // console (action_respond_info). filament_at_extruder is the same toolhead
      // motion-sensor signal the macro checks (eN_filament.filament_detected).
      const cur = state.mode || "normal";
      if ((cur === "normal") !== (m === "normal")) {
        const loaded = (state.toolheads || []).filter(th => th.filament_at_extruder);
        if (loaded.length) {
          const heads = loaded.map(th => th.name || ("T" + th.idx)).join(", ");
          confirm({
            title: t("ui.config.mode_locked_title"),
            message: t("ui.config.mode_locked_msg", {heads}),
            okLabel: "OK",
            dismissOnly: true,
          });
          return;
        }
      }
      const args = {MODE: m};
      confirm({
        title: t("ui.dialog.switch_mode_title", {mode: m}),
        message: t("ui.dialog.switch_mode_msg", {mode: m}),
        okLabel: t("ui.dialog.switch"),
        onOk: async () => {
          // No web reboot banner for a mode change: a transition crossing
          // 'normal' makes ACE_RUN_MODE_SWITCH raise a reboot error, which
          // shows on the touchscreen popup AND Fluidd AND the web - for both
          // directions and any trigger (web or Fluidd SET_ACE_MODE), unlike a
          // web-only banner. multi<->head is a runtime flip and raises nothing.
          await run("SET_ACE_MODE", args);
        },
      });
    }
    const screenCanvas = ref(null);
    const floatScreenCanvas = ref(null);
    const screenPopout = ref(false);
    const screenFps = ref(0);
    const screenEtag = ref("");
    let frameCount = 0;
    let lastFpsTs = performance.now();
    let pollScreenBusy = false;
    function _liveScreenCanvases() {
      return [screenCanvas.value, floatScreenCanvas.value].filter(Boolean);
    }
    async function pollScreen() {
      if (pollScreenBusy) return;
      const targets = _liveScreenCanvases();
      if (!targets.length) return;
      pollScreenBusy = true;
      try {
        const headers = {};
        if (screenEtag.value) headers["If-None-Match"] = `"${screenEtag.value}"`;
        const r = await fetch(`${SCREEN}/snapshot`, {headers, cache: "no-store"});
        if (r.status === 304) {  }
        else if (r.ok) {
          screenEtag.value = (r.headers.get("ETag") || "").replace(/"/g, "");
          const blob = await r.blob();
          const img = await createImageBitmap(blob);
          for (const c of targets) {
            if (img.width !== c.width || img.height !== c.height) {
              c.width = img.width;
              c.height = img.height;
            }
            c.getContext("2d").drawImage(img, 0, 0);
          }
          frameCount += 1;
          const now = performance.now();
          if (now - lastFpsTs >= 1000) {
            screenFps.value = (frameCount * 1000) / (now - lastFpsTs);
            frameCount = 0;
            lastFpsTs = now;
          }
        }
      } catch (_) {  }
      finally { pollScreenBusy = false; }
    }
    function screenCoords(ev) {
      const c = ev.currentTarget;
      const rect = c.getBoundingClientRect();
      return {
        x: Math.round((ev.clientX - rect.left) * c.width / rect.width),
        y: Math.round((ev.clientY - rect.top) * c.height / rect.height),
      };
    }
    async function sendTouch(action, x, y) {
      try { await fetch(`${SCREEN}/touch?a=${action}&x=${x}&y=${y}`, {method: "POST"}); } catch (_) {}
    }
    function screenDown(ev) {
      ev.currentTarget?.setPointerCapture?.(ev.pointerId);
      const {x, y} = screenCoords(ev); sendTouch("down", x, y);
    }
    function screenMove(ev) {
      if (ev.buttons === 0) return;
      const {x, y} = screenCoords(ev); sendTouch("move", x, y);
    }
    function screenUp(ev) {
      const {x, y} = screenCoords(ev); sendTouch("up", x, y);
    }
    function toggleScreenPopout() {
      screenPopout.value = !screenPopout.value;
    }
    const popoutPos = reactive({
      x: parseFloat(localStorage.getItem("multiace.popout.x")) || null,
      y: parseFloat(localStorage.getItem("multiace.popout.y")) || null,
    });
    const popoutStyle = computed(() => {
      if (popoutPos.x == null || popoutPos.y == null) return {};
      return {
        left: popoutPos.x + "px",
        top:  popoutPos.y + "px",
        right: "auto",
        bottom: "auto",
      };
    });
    let _popoutDrag = null;
    function popoutDragStart(ev) {
      if (ev.target.closest(".screen-popout-close")) return;
      const panel = ev.currentTarget.parentElement;
      const rect = panel.getBoundingClientRect();
      _popoutDrag = {
        offX: ev.clientX - rect.left,
        offY: ev.clientY - rect.top,
        panel,
      };
      ev.currentTarget.setPointerCapture?.(ev.pointerId);
      ev.preventDefault();
    }
    function popoutDragMove(ev) {
      if (!_popoutDrag) return;
      const p = _popoutDrag;
      const w = p.panel.offsetWidth;
      const h = p.panel.offsetHeight;
      const maxX = window.innerWidth - w;
      const maxY = window.innerHeight - h;
      popoutPos.x = Math.max(0, Math.min(maxX, ev.clientX - p.offX));
      popoutPos.y = Math.max(0, Math.min(maxY, ev.clientY - p.offY));
    }
    function popoutDragEnd(ev) {
      if (!_popoutDrag) return;
      _popoutDrag = null;
      ev.currentTarget?.releasePointerCapture?.(ev.pointerId);
      if (popoutPos.x != null) localStorage.setItem("multiace.popout.x", String(popoutPos.x));
      if (popoutPos.y != null) localStorage.setItem("multiace.popout.y", String(popoutPos.y));
    }
    let ws = null;
    let wsReconnectTimer = null;
    function wsConnect() {
      try { ws = new WebSocket(WS_URL); }
      catch (e) { conn.value = {state: "err", text: `WS: ${e}`}; scheduleReconnect(); return; }
      ws.onopen = () => {
        conn.value = {state: "ok", text: t("ui.header.live")};
        // Console lines are opt-in: only ask for them while the pane
        // that shows them is actually open.
        _sendConsoleSubscription();
      };
      ws.onmessage = (ev) => {
        debugPanel.messages++;
        debugPanel.lastMessage = Date.now();
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "state") { applyState(m); debugPanel.mock = !!m.mock; }
          else if (m.type === "console") _appendConsole(m.lines || []);
          else if (m.type === "gcode_error") onGcodeError(m);
          else if (m.type === "error") conn.value = {state: "warn", text: m.error || t("ui.header.ws_error")};
        } catch (_) {}
      };
      ws.onclose = () => { conn.value = {state: "warn", text: t("ui.header.offline")}; scheduleReconnect(); };
      ws.onerror = () => { conn.value = {state: "err", text: t("ui.header.ws_error")}; };
    }
    function scheduleReconnect() {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = setTimeout(wsConnect, 3000);
    }
    let screenTimer = null;
    function _updateScreenTimer() {
      clearInterval(screenTimer);
      const wantPoll = screenAvailable.value && screenPopout.value;
      if (wantPoll) screenTimer = setInterval(pollScreen, 200);
    }
    watch([screenPopout, screenAvailable], _updateScreenTimer, {immediate: true});
    const uploading = ref(false);
    const uploadInput = ref(null);
    const preflight = reactive({
      open:    false,
      busy:    false,
      sending: "",
      // Non-empty while "apply loadout" is writing slot-overrides / feeder
      // identities (the plan key being applied).
      applying: "",
      report:  null,
      error:   "",
      progress: null,
      // Manual slot reassignment for the slicer plan only: {origT: "ace-slot"}.
      // slicerSwaps holds the recomputed swap count (null = use the plan's
      // server value); recomputed automatically on every dropdown change
      // (the old recalc button is gone, Dirk 2026-08-14).
      slicerOverrides: {},
      slicerSwaps: null,
      // Head mode: same idea for the single colour->target table. headOverrides
      // is {origT: target_id} ("feeder-N" / "slot-A-S"); headSwaps the recomputed
      // ACE-head swap count (null = use the server plan value).
      headOverrides: {},
      headSwaps: null,
      // Print-preference toggles (default off): inject SET_PRINT_PREFERENCES so
      // an upload/SD start runs bed mesh / timelapse camera (stock only does
      // these on the official start).
      bedMesh: false,
      camera:  false,
      // true when this report was produced in-browser (Pyodide worker) rather
      // than by the printer backend - selects the local rewrite+upload path.
      local: false,
    });
    function triggerUpload() { uploadInput.value && uploadInput.value.click(); }
    function tierLabel(tier) {
      const t_map = {
        exact_hex:        "exact",
        name_exact:       "name",
        name_base:        "name·base",
        name_canon:       "name·synonym",
        fuzzy:            "fuzzy",
        fallback:         "fallback ⚠",
        duplicate:        "duplicate ⚠",
        no_slot:          "no slot ⚠",
      };
      return t_map[tier] || tier;
    }
    function tierWarn(tier) {
      return tier && (tier === "fallback"
                      || tier === "duplicate"
                      || tier === "no_slot");
    }
    function rgbDec(hex) {
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return `${r},${g},${b}`;
    }
    function sortedMapping(plan) {
      const rows = (plan && plan.mapping) || [];
      return rows.slice().sort((a, b) => {
        const sa = a.slot, sb = b.slot;
        if (!sa && !sb) return a.t - b.t;
        if (!sa) return  1;
        if (!sb) return -1;
        if (sa.ace !== sb.ace)   return sa.ace  - sb.ace;
        if (sa.slot !== sb.slot) return sa.slot - sb.slot;
        return a.t - b.t;
      });
    }
    // --- FOrca mixed-nozzle view -----------------------------------------
    // A mixed-nozzle file is NOT an assignment problem: the slicer baked each
    // tool's own line width into that tool's extrusions, so a colour may only
    // print on a head carrying the diameter it was sliced for (the backend's
    // nozzle gate enforces it). What is left to decide is which ACE a spool
    // sits in - not which slot index. So this view replaces the normal
    // assignment table with a per-DIAMETER loading instruction.
    function forcaMixed() {
      const r = preflight.report;
      return !!(r && r.forca && r.nozzles_mixed);
    }
    function forcaNozzleList() {
      // [{head, dia}] in head order - the PRINTER's nozzles, not the file's.
      // The file's `nozzle_diameter` is indexed per FILAMENT (demand); which
      // head carries which nozzle is the machine's business (supply). Falls
      // back to reading the file's first four entries as heads when the
      // printer could not be asked - the pre-2026-08-07 reading.
      const r = preflight.report || {};
      const hn = r.head_nozzles || {};
      const src = Object.keys(hn).length ? hn : (r.nozzles || {});
      return Object.keys(src)
        .map(k => ({head: parseInt(k, 10), dia: Number(src[k])}))
        .filter(e => !isNaN(e.head) && e.dia > 0 && e.head < 4)
        .sort((a, b) => a.head - b.head);
    }
    function forcaToolDia(tt) {
      // Which nozzle diameter tool `tt` was sliced for, or null if the file
      // does not say (an unassigned filament - the list can be shorter than
      // the filament count, seen on a file whose 5th filament had no nozzle
      // wired yet).
      const nz = (preflight.report && preflight.report.nozzles) || {};
      const d = nz[String(tt)];
      return (d === undefined || d === null) ? null : Number(d);
    }
    // Swap count for the FOrca view. That view REPLACES the normal plan
    // tables, and with them the only place the count was rendered - so a
    // mixed-nozzle file never told the user how many tool changes its
    // assignment costs (Dirk 2026-08-09). No new arithmetic: the FOrca
    // rows edit the very plan the existing counters already track
    // (loadout in head mode, slicer in multi), including the live recalc
    // after a dropdown change.
    function forcaSwapsDisplay() {
      const r = preflight.report;
      if (!r) return null;
      return r.head_mode ? headSwapsDisplay() : slicerSwapsDisplay();
    }
    // Which plan the FOrca rows edit - the same one forcaGroups() reads.
    function forcaPlanKey() {
      const r = preflight.report;
      return (r && r.head_mode) ? "loadout" : "slicer";
    }
    // The rest of the plan header's info line, reusing the plan readers
    // verbatim (Dirk 2026-08-09: "einfach die Anzeige, optimieren nicht").
    // bg-swaps only in head mode - background swaps REQUIRE the 1:1
    // head<->ACE wiring, in multi one ACE feeds several heads and the
    // number would be meaningless (S36: hardware, not policy).
    function forcaBgLabel() {
      const r = preflight.report;
      if (!r || !r.head_mode) return "";
      return headPlanBgLabel(forcaPlanKey());
    }
    function forcaFlushG() {
      return headPlanFlushG(forcaPlanKey());
    }
    function forcaGroups() {
      // One section per nozzle DIAMETER, NAMED after the head(s) that carry
      // it. The two possible groupings answer different questions - per size
      // is the CONSTRAINT ("these colours need a 0.4 head"), per head is the
      // ACTION ("these spools go into ACE 3") - and which one is right
      // depends on whether the size is shared:
      //   one head per size  -> the section has no freedom in it, so naming
      //                         it by the size names an abstraction the user
      //                         then has to translate back (Dirk 2026-08-08:
      //                         "per nozzle size ist verwirrend")
      //   two heads per size -> the freedom is real, and splitting them into
      //                         two head sections would present our pick as
      //                         if it were fixed
      // Grouping by size keeps the pool visible; the heading says "Head 3"
      // or "Head 1 + 2", so the common case reads as the action it is.
      const r = preflight.report;
      if (!r) return [];
      const nz = forcaNozzleList();
      const cols = (r.slicer_colors || []);
      // Current assignment, whichever mode produced it.
      const plan = r.head_mode ? (r.plans && r.plans.loadout)
                               : (r.plans && r.plans.slicer);
      const byT = {};
      ((plan && plan.mapping) || []).forEach(m => { byT[m.t] = m; });
      // One group per nozzle diameter the MACHINE offers, plus a catch-all for
      // colours whose nozzle the file does not declare. Grouping by the
      // machine's supply (not the file's per-filament list) is what makes
      // "several colours share one head" render correctly - and it is the
      // normal case for a feature split.
      const groups = {};
      nz.forEach(e => {
        const key = String(e.dia);
        if (!groups[key]) groups[key] = {dia: e.dia, heads: [], colors: []};
        groups[key].heads.push(e.head);
      });
      Object.keys(groups).forEach(k => {
        groups[k].heads.sort((a, b) => a - b);
        groups[k].head = groups[k].heads[0];   // sort key + section key
      });
      const unknown = {head: null, dia: null, heads: [], colors: []};
      cols.forEach(c => {
        const want = forcaToolDia(c.t);
        // A colour with no declared nozzle must be SHOWN, not dropped: it
        // used to vanish from the view entirely (a file with more filaments
        // than nozzle entries). It is also unconstrained by the gate, so the
        // user has to see that it needs a decision.
        const g = (want === null) ? unknown : groups[String(want)];
        if (!g) {
          // Declared a diameter no head on this machine carries.
          unknown.colors.push({
            t: c.t, hex: c.hex, name: c.name, material: c.material,
            slot: null, tier: "no_slot", ok: false, foundAt: null,
            wantDia: want, noHead: true,
          });
          return;
        }
        const m = byT[c.t] || {};
        // Unassigned does NOT mean "absent". The usual case is that the
        // spool IS loaded, just in a lane feeding a different nozzle size -
        // telling the user "not loaded" would send them hunting for a spool
        // sitting right in front of them. So look the colour up among the
        // live slots and report where it actually is.
        let foundAt = null;
        if (!m.slot) {
          const wantHex = (c.hex || "").toLowerCase().replace(/^#/, "");
          const wantMat = (c.material || "").toLowerCase();
          foundAt = (r.live_slots || []).find(s => {
            const have = (s.color || "").toLowerCase().replace(/^#/, "");
            const haveMat = (s.material || "").toLowerCase();
            return wantHex && have === wantHex
              && (!wantMat || !haveMat || haveMat === wantMat);
          }) || null;
        }
        g.colors.push({
          t: c.t, hex: c.hex, name: c.name, material: c.material,
          slot: m.slot || null, tier: m.tier || "no_slot",
          ok: !!(m.slot), foundAt: foundAt,
        });
      });
      const out = Object.keys(groups)
        .map(k => groups[k])
        .sort((a, b) => a.head - b.head);
      if (unknown.colors.length) out.push(unknown);
      return out;
    }
    function forcaGroupAces(g) {
      // HEAD MODE only: each head is wired 1:1 to one ACE, so a section that
      // names its head can name the unit the spools physically go into - the
      // whole point of the view. In MULTI the ACE is the free axis (slot N
      // feeds head N across all units), so naming one there would be a lie;
      // returns null and the heading stays head + size.
      const r = preflight.report;
      if (!r || !r.head_mode) return null;
      const ha = state.head_ace || {};
      const out = (g.heads || []).map(h => {
        const a = ha[h] ?? ha[String(h)];
        return dispIdx((a === undefined || a === null) ? h : Number(a));
      });
      return out.length ? out : null;
    }
    function forcaTargetHint(g, c) {
      // What the user physically has to do - and that is the OPPOSITE axis in
      // the two modes, so a single wording is wrong in one of them:
      //   MULTI     head == slot index -> the SLOT is fixed, the ACE is free
      //   HEAD MODE the ACE is wired to the head -> the ACE is fixed, any of
      //             its slots will do
      // Saying "head N" in multi is technically true but useless: spools go
      // into slots, not heads.
      const r = preflight.report;
      const heads = g.heads || [];
      if (!heads.length) {
        // Catch-all group: no head to name, and the REASON is per colour, not
        // per group - it mixes "the file names no nozzle" with "it names one
        // this machine does not have", and the latter differs by diameter.
        // Reading it off g.colors[0] made all rows show the first row's
        // diameter (HW screenshot: three rows all claiming 0.8 mm while two
        // of them needed 0.2).
        const cc = c || {};
        return cc.noHead
          ? t("ui.preflight.forca_dia_absent", {dia: cc.wantDia})
          : t("ui.preflight.forca_undeclared");
      }
      if (r && r.head_mode) {
        const aces = heads.map(h => {
          const ha = state.head_ace || {};
          const a = ha[h] ?? ha[String(h)];
          return dispIdx((a === undefined || a === null) ? h : Number(a));
        });
        return t("ui.preflight.forca_target_head",
                 {ace: aces.join("/"), head: heads.map(h => dispIdx(h)).join("/")});
      }
      return t("ui.preflight.forca_target_multi",
               {slot: heads.map(h => dispIdx(h)).join("/")});
    }
    function forcaFeasible() {
      // Judge the EFFECTIVE choice (base plan + the user's dropdown), not the
      // backend's initial assignment - otherwise picking a substitute spool
      // would leave the print button dead.
      const head = !!(preflight.report && preflight.report.head_mode);
      return forcaGroups().every(g => g.colors.every(c => head
        ? !!headEffectiveTargetId(c.t)
        : !!slicerEffectiveSlot(c.t)));
    }
    function slicerColorsInPrintOrder() {
      // The "Slicer colors" list ordered by FIRST use in the print (Dirk
      // 2026-07-19: fold the print order into the existing list instead of
      // a separate chip strip). First-appearance index from report.events
      // (the toolchange sequence); colours the body never prints keep
      // their T order after the used ones. No events -> original order.
      const cols = (preflight.report && preflight.report.slicer_colors) || [];
      const evs = (preflight.report && preflight.report.events) || [];
      const first = {};
      evs.forEach((t, i) => { if (!(t in first)) first[t] = i; });
      return cols.slice().sort((a, b) => {
        const fa = (a.t in first) ? first[a.t] : Infinity;
        const fb = (b.t in first) ? first[b.t] : Infinity;
        if (fa !== fb) return fa - fb;
        return a.t - b.t;
      });
    }
    // --- slicer-plan manual slot reassignment ---------------------------
    function slotKey(slot) { return slot ? (slot.ace + "-" + slot.slot) : ""; }
    function textOn(hex) {
      // Readable text colour (dark/light) for a coloured background, by
      // perceived luminance. Unknown/short colour -> neutral light text.
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "#e8e8e8";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#111" : "#fff";
    }
    function _liveSlotByKey(key) {
      const [a, s] = (key || "").split("-").map(Number);
      return (preflight.report?.live_slots || [])
        .find(ls => ls.ace === a && ls.slot === s) || null;
    }
    function _slicerColorMat(tt) {
      const c = (preflight.report?.slicer_colors || []).find(x => x.t === tt);
      return ((c && c.material) || "").trim().toLowerCase();
    }
    function slicerSlotOptions(tt) {
      // Only loaded slots whose material matches the slicer-T (material-strict,
      // mirrors the auto-matcher / CLAUDE.md §23).
      //
      // Under a mixed-nozzle file the option list is additionally restricted
      // to slots feeding a head of the RIGHT nozzle size - but it stays a
      // free choice within that (Dirk: "wenn nicht geladen ist kann ich doch
      // eine andere farbe aussuchen"). The nozzle fixes which HEAD a tool
      // prints on, never which COLOUR the user wants there; picking a
      // different spool is legitimate and the auto-load block loads it.
      const mat = _slicerColorMat(tt);
      const allowed = forcaAllowedHeads(tt);
      return (preflight.report?.live_slots || []).filter(ls => {
        const m = (ls.material || "").trim().toLowerCase();
        if (mat && m && m !== mat) return false;
        if (allowed && !allowed.includes(headOfSlot(ls))) return false;
        return true;
      });
    }
    function headOfSlot(ls) {
      // Which head a loaded slot feeds - the same modus split the backend
      // gate makes (_head_of). MULTI: head == slot index. HEAD MODE: the
      // head wired to that ACE, via the EXISTING reverse lookup
      // (head_ace maps head -> ace, not ace -> head; reading it the other
      // way round would silently return the wrong head).
      const r = preflight.report;
      if (r && r.head_mode) return aceHeadForAce(Number(ls.ace));
      return ls.slot;
    }
    function forcaAllowedHeads(tt) {
      // The heads that CARRY the diameter this tool needs - demand from the
      // file, supply from the machine. null = no restriction (not FOrca,
      // uniform machine, or the file does not declare this tool's nozzle).
      if (!forcaMixed()) return null;
      const want = forcaToolDia(tt);
      if (want === null) return null;
      return forcaNozzleList()
        .filter(e => Math.abs(e.dia - want) < 0.001)
        .map(e => e.head);
    }
    function unsetSlotsForT(tt) {
      // Occupied slots the preflight may NOT use because nobody declared what
      // is in them (no tag, no override - the card shows them as "Job" or
      // blank). They used to be omitted silently, which reads as "the slot
      // does not exist"; listing them greyed out says WHY they are unusable.
      // Read straight from the dashboard state - live_slots has already
      // dropped them, and it must keep doing so (an undeclared slot must
      // never become a match target).
      const out = [];
      const allowed = forcaAllowedHeads(tt);
      (state.aces || []).forEach(a => {
        const head = aceHeadForAce(a.idx);
        if (preflight.report && preflight.report.head_mode) {
          if (head === null) return;                 // ACE no head is wired to
          if (allowed && !allowed.includes(head)) return;
        }
        (a.slots || []).forEach(s => {
          if (s.state === "empty") return;
          if (s.source === "rfid" || s.source === "override") return;
          out.push({ace: a.idx, slot: s.idx});
        });
      });
      return out;
    }
    function forcaHeadsForMaterial(mat) {
      // Which heads a MISSING material would have to be loaded on: union of
      // the allowed heads of every slicer colour asking for it. Naming the
      // head turns "PETG missing" into an instruction - under mixed nozzles
      // the material can only go on the head that carries the right size, so
      // "load PETG somewhere" is not actionable on its own.
      // [] = the file declares no nozzle for those colours (no constraint),
      // null = not a mixed-nozzle file at all.
      if (!forcaMixed()) return null;
      const want = (mat || "").trim().toLowerCase();
      const heads = new Set();
      (((preflight.report || {}).slicer_colors) || []).forEach(c => {
        if ((c.material || "").trim().toLowerCase() !== want) return;
        (forcaAllowedHeads(c.t) || []).forEach(h => heads.add(h));
      });
      return Array.from(heads).sort((a, b) => a - b);
    }
    function _slicerBaseSlot(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.slot) || null;
    }
    function slicerEffectiveSlot(tt) {
      const ov = preflight.slicerOverrides[tt];
      return ov ? _liveSlotByKey(ov) : _slicerBaseSlot(tt);
    }
    function onSlicerSlotChange(tt, key) {
      const base = _slicerBaseSlot(tt);
      if (base && slotKey(base) === key) delete preflight.slicerOverrides[tt];
      else preflight.slicerOverrides[tt] = key;
      recalcSlicer();
    }
    function _slicerEffectiveMapping() {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return ((plan && plan.mapping) || [])
        .map(m => ({t: m.t, slot: slicerEffectiveSlot(m.t)}));
    }
    function realSwapCount(events, mapping) {
      // Port of backend _real_swap_count: replay the toolchange T-sequence,
      // initial loadout = slot==head per head, count head re-(ace,slot) changes.
      const byT = {};
      for (const m of mapping) if (m.slot) byT[m.t] = m.slot;
      const head = {0: [0, 0], 1: [0, 1], 2: [0, 2], 3: [0, 3]};
      let swaps = 0;
      for (const tt of (events || [])) {
        const slot = byT[tt];
        if (!slot) continue;
        const cur = head[slot.slot];
        if (!cur || cur[0] !== slot.ace || cur[1] !== slot.slot) {
          swaps++;
          head[slot.slot] = [slot.ace, slot.slot];
        }
      }
      return swaps;
    }
    function recalcSlicer() {
      preflight.slicerSwaps = realSwapCount(
        (preflight.report && preflight.report.events) || [],
        _slicerEffectiveMapping());
      recalcEstimate("slicer", {mapping: _slicerEffectiveMapping()});
    }
    // Re-run the §1 time/cost estimate for the given plan against the
    // user's edited mapping/assignment - the money/time counterpart of the
    // swap-count recompute above, which only ever touched `swaps`. Only the
    // Pyodide (local) preflight keeps the estimate ctx needed for this
    // (captured per-job in the worker); the server-fallback path still
    // shows the estimate for the original mapping, same as before this.
    // Superseded requests are dropped by sequence number so a burst of
    // dropdown clicks can't let a stale reply land after a newer one.
    let _estimateReqSeq = 0;
    async function recalcEstimate(planKey, payload) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans[planKey];
      if (!plan || !preflight.local || !preflightWorker || !preflightJobId) return;
      const seq = ++_estimateReqSeq;
      try {
        const msg = await runPreflightWorker("estimate", Object.assign(
          {jobId: preflightJobId}, payload));
        if (seq !== _estimateReqSeq) return;
        if (msg.estimate !== undefined) plan.estimate = msg.estimate;
        if (msg.timeline !== undefined) plan.timeline = msg.timeline;
      } catch (_) {
        // Informational only - leave the previous numbers on a worker hiccup
        // rather than erroring the whole preflight over the estimate.
      }
    }
    function slicerSwapsDisplay() {
      if (preflight.slicerSwaps !== null) return preflight.slicerSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return (plan && plan.swaps) || 0;
    }
    // --- head-mode colour -> target (feeder pin / ACE slot) assignment -----
    function headTargets() {
      return (preflight.report && preflight.report.targets) || [];
    }
    function _headTargetById(id) {
      return headTargets().find(tg => tg.id === id) || null;
    }
    function headTargetOptions(tt) {
      // Only targets whose material matches the slicer-T (material-strict,
      // mirrors compute_head_mode_layout's pre-filter). Empty material on
      // either side is treated as a wildcard.
      // Under a mixed-nozzle file also restricted to targets on a head of
      // the right nozzle size - a pin target IS a head, an ace target's head
      // comes from the wiring (see headOfSlot). The choice stays free within
      // that set, exactly like the multi dropdown.
      const mat = _slicerColorMat(tt);
      const allowed = forcaAllowedHeads(tt);
      return headTargets().filter(tg => {
        const m = (tg.material || "").trim().toLowerCase();
        if (mat && m && m !== mat) return false;
        if (allowed) {
          const h = (tg.kind === "pin") ? tg.head : aceHeadForAce(Number(tg.ace));
          if (h === null || !allowed.includes(h)) return false;
        }
        return true;
      });
    }
    function _headBaseTargetId(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.target_id) || "";
    }
    function headEffectiveTargetId(tt) {
      const ov = preflight.headOverrides[tt];
      return ov !== undefined ? ov : _headBaseTargetId(tt);
    }
    function headTargetLabel(tg) {
      if (!tg) return "";
      const mat = tg.material || "?";
      if (tg.kind === "pin") return t("ui.preflight.feeder") + " " + dispIdx(tg.head) + " · " + mat;
      return "ACE " + dispIdx(tg.ace) + " Slot " + dispIdx(tg.slot) + " · " + mat;
    }
    function headTargetColor(id) {
      const tg = _headTargetById(id);
      return (tg && tg.color) || "#444";
    }
    function headTargetLabelById(id) {
      const tg = _headTargetById(id);
      return tg ? headTargetLabel(tg) : "";
    }
    // Custom dropdown for the head-mode target picker: native <option>s
    // cannot render a colour chip NEXT to a label (only full-background
    // fills, which were loud/uneven - Dirk 2026-07-10), so the open list is
    // a small custom popup with chip + label per entry. One open at a time,
    // keyed by the slicer-T; items pick on mousedown (fires before the
    // button's blur closes the list).
    const hmDropOpen = ref(null);
    // .cmap-pair clips to a rounded card (overflow:hidden, for the colour
    // swatches) - an absolutely-positioned popup inside it would be clipped
    // to that tiny card instead of overlaying the page, cutting the option
    // list down to a sliver (same class of bug .lang-dd-list already works
    // around). Fixed positioning escapes the clip; the trade-off is we have
    // to compute where "under the button" is ourselves.
    const hmDropPos = reactive({top: 0, left: 0, width: 0});
    function hmDdToggle(tt, evt) {
      if (hmDropOpen.value === tt) { hmDropOpen.value = null; return; }
      const btn = evt && evt.currentTarget;
      if (btn) {
        const r = btn.getBoundingClientRect();
        hmDropPos.top = r.bottom + 2;
        hmDropPos.left = r.left;
        hmDropPos.width = r.width;
      }
      hmDropOpen.value = tt;
    }
    function hmDdClose() { hmDropOpen.value = null; }
    // The popup is position:fixed, so it does not move with whatever
    // scrolled (the cmap-strip's own horizontal scroll, or the page) -
    // closing on any scroll is simpler and less surprising than re-tracking
    // the anchor on every frame.
    function _hmDdScrollClose() { if (hmDropOpen.value !== null) hmDdClose(); }
    onMounted(() => {
      window.addEventListener("scroll", _hmDdScrollClose, true);
      window.addEventListener("resize", _hmDdScrollClose);
    });
    onUnmounted(() => {
      window.removeEventListener("scroll", _hmDdScrollClose, true);
      window.removeEventListener("resize", _hmDdScrollClose);
    });
    function hmDdPick(tt, id) {
      hmDropOpen.value = null;
      onHeadTargetChange(tt, id);
    }
    function onHeadTargetChange(tt, id) {
      const base = _headBaseTargetId(tt);
      if (id === base) delete preflight.headOverrides[tt];
      else preflight.headOverrides[tt] = id;
      recalcHead();
    }
    function _headEffectiveAssignment() {
      // {origT: target_id} across every slicer colour (base plan + overrides).
      const out = {};
      for (const c of ((preflight.report && preflight.report.slicer_colors) || [])) {
        out[c.t] = headEffectiveTargetId(c.t);
      }
      return out;
    }
    function headSwapCount(events, assignment) {
      // Port of backend head_mode_swap_count: only ACE-target (ace,slot)
      // changes count, PER ACE head (each ACE head swaps independently);
      // pinned feeder colours never swap.
      const cur = {};
      let swaps = 0;
      for (const tt of (events || [])) {
        const tg = _headTargetById(assignment[tt]);
        if (!tg || tg.kind !== "ace") continue;
        const key = tg.ace + "-" + tg.slot;
        if (cur[tg.head] !== key) { swaps++; cur[tg.head] = key; }
      }
      return swaps;
    }
    function recalcHead() {
      preflight.headSwaps = headSwapCount(
        (preflight.report && preflight.report.events) || [],
        _headEffectiveAssignment());
      recalcEstimate("loadout", {targetIds: _headEffectiveAssignment()});
    }
    function headSwapsDisplay() {
      if (preflight.headSwaps !== null) return preflight.headSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      return (plan && plan.swaps) || 0;
    }
    function headFeasible() {
      // Every slicer colour must resolve to a real target (no unassigned row).
      const asn = _headEffectiveAssignment();
      return Object.values(asn).every(id => !!_headTargetById(id));
    }
    // --- the three head plans (loadout editable, optimize/layer proposed) -----
    function headPlanFeasible(hp) {
      if (hp === "loadout") return headFeasible();
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return !!(p && p.feasible);
    }
    function headPlanSwaps(hp) {
      if (hp === "loadout") return headSwapsDisplay();
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && p.swaps) || 0;
    }
    // Background-unload balance of a head-mode plan (server-computed;
    // stale after loadout edits like the swap count - same stale marker).
    function headPlanBg(hp) {
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && p.bg && p.bg.unloads > 0) ? p.bg : null;
    }
    // Same-nozzle contamination volume of a head plan, as grams (server-
    // computed from the slicer flush matrix; null = no matrix in the file).
    function headPlanFlushG(hp) {
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && typeof p.flush_g === "number") ? p.flush_g : null;
    }
    function headPlanBgLabel(hp) {
      const bg = headPlanBg(hp);
      if (!bg) return "";
      let s = t('ui.preflight.bg_label') + " " + bg.bg_ok + "/" + bg.unloads;
      const min = Math.round((bg.saved_s || 0) / 60);
      if (bg.bg_ok > 0 && min > 0) {
        s += " (~" + min + " min " + t('ui.preflight.bg_saved') + ")";
      }
      // Why the rest does NOT qualify - the diagnosis Dirk was missing
      // (">4 colours and still no benefit": short windows vs chain on a
      // non-BG head vs missing M73 look different here).
      const parts = [];
      if (bg.bg_small)    parts.push(bg.bg_small + " " + t('ui.preflight.bg_too_short'));
      if (bg.bg_disabled) parts.push(bg.bg_disabled + " " + t('ui.preflight.bg_not_enabled'));
      if (bg.bg_unknown)  parts.push(bg.bg_unknown + " " + t('ui.preflight.bg_no_m73'));
      if (parts.length) s += " · " + parts.join(", ");
      return s;
    }
    function _headSlicerColor(tt) {
      return ((preflight.report && preflight.report.slicer_colors) || [])
        .find(c => c.t === tt) || null;
    }
    function headSlicerHex(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.hex) || "#444";
    }
    function headSlicerMat(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.material) || "?";
    }
    function headProposalLabel(m) {
      // The proposed destination for a slicer colour (load that colour here).
      if (!m || m.kind === "none") return "";
      if (m.kind === "pin") return t("ui.preflight.feeder") + " " + dispIdx(m.head);
      return "ACE " + dispIdx(m.ace) + " Slot " + dispIdx(m.slot);
    }
    // =================================================================
    // Colour map (redesign plan section 5): one row model for all three
    // modes (normal/multi, head, FOrca). No new matching logic - this only
    // changes how pp.match_colors_to_slots's result is SHOWN, and makes the
    // override affordance identical everywhere.
    //   row = {t, src:{hex,name,material}, dst:{...}|null, tier,
    //          options:[{id,color,label}], unset:[{ace,slot}], editable,
    //          group:{key,label,hint}|null, changed}
    // =================================================================
    const cmapDetails = ref(false);
    function _rowsNormal() {
      const r = preflight.report;
      return slicerColorsInPrintOrder().map(c => {
        const eff = slicerEffectiveSlot(c.t);
        const base = ((r.plans.slicer && r.plans.slicer.mapping) || [])
          .find(m => m.t === c.t);
        return {
          t: c.t,
          src: {hex: c.hex, name: c.name, material: c.material},
          dst: eff ? {
            id: slotKey(eff), kind: "slot", color: eff.color,
            material: eff.material,
            label: "ACE " + dispIdx(eff.ace) + " / " + dispIdx(eff.slot),
          } : null,
          tier: (base && base.tier) || "no_slot",
          options: slicerSlotOptions(c.t).map(o => ({
            id: slotKey(o), color: o.color,
            label: "ACE " + dispIdx(o.ace) + " / " + dispIdx(o.slot)
                  + " · " + (o.material || "?"),
          })),
          unset: unsetSlotsForT(c.t),
          editable: true,
          group: null,
        };
      });
    }
    function _rowsHead() {
      const r = preflight.report;
      const loadoutMap = (r.plans.loadout && r.plans.loadout.mapping) || [];
      return slicerColorsInPrintOrder().map(c => {
        const id = headEffectiveTargetId(c.t);
        const tg = id ? _headTargetById(id) : null;
        const base = loadoutMap.find(m => m.t === c.t);
        return {
          t: c.t,
          src: {hex: c.hex, name: c.name, material: c.material},
          dst: tg ? {
            id: tg.id, kind: tg.kind, color: tg.color, material: tg.material,
            label: headTargetLabel(tg),
          } : null,
          tier: (base && base.tier) || "no_slot",
          options: headTargetOptions(c.t).map(o => ({
            id: o.id, color: o.color, label: headTargetLabel(o),
          })),
          unset: unsetSlotsForT(c.t),
          editable: true,
          group: null,
        };
      });
    }
    function _forcaColorRow(c, group) {
      const r = preflight.report;
      const head = !!(r && r.head_mode);
      let dst = null, options;
      if (head) {
        const id = headEffectiveTargetId(c.t);
        const tg = id ? _headTargetById(id) : null;
        if (tg) dst = {id: tg.id, kind: tg.kind, color: tg.color,
                       material: tg.material, label: headTargetLabel(tg)};
        options = headTargetOptions(c.t).map(o => ({
          id: o.id, color: o.color, label: headTargetLabel(o)}));
      } else {
        const eff = slicerEffectiveSlot(c.t);
        if (eff) dst = {id: slotKey(eff), kind: "slot", color: eff.color,
                        material: eff.material,
                        label: "ACE " + dispIdx(eff.ace) + " / " + dispIdx(eff.slot)};
        options = slicerSlotOptions(c.t).map(o => ({
          id: slotKey(o), color: o.color,
          label: "ACE " + dispIdx(o.ace) + " / " + dispIdx(o.slot)
                + " · " + (o.material || "?")}));
      }
      return {
        t: c.t, src: {hex: c.hex, name: c.name, material: c.material},
        dst, tier: c.tier, options, unset: unsetSlotsForT(c.t),
        editable: true, group,
      };
    }
    function _rowsForca() {
      const out = [];
      forcaGroups().forEach(g => {
        const label = g.heads.length
          ? t("ui.preflight.head") + " " + g.heads.map(h => dispIdx(h)).join(" + ")
          : t("ui.preflight.forca_no_head");
        const hint = g.dia ? (g.dia + " mm") : "";
        const key = String(g.head !== null && g.head !== undefined ? g.head : "x" + hint);
        g.colors.forEach(c => out.push(_forcaColorRow(c, {key, label, hint})));
      });
      return out;
    }
    // The strip shows a PROPOSAL read-only when a non-base strategy is
    // selected - the auto matcher's overrides only ever apply to the base
    // (as-loaded) plan, so pretending the strip is still editable on the
    // other tabs would silently do nothing (plan section 5.3).
    function _rowsForPlan(key) {
      const r = preflight.report;
      const plan = r.plans[key];
      if (!plan) return [];
      const head = !!r.head_mode;
      const baseRows = head ? _rowsHead()
                    : (forcaMixed() ? _rowsForca() : _rowsNormal());
      const baseByT = {};
      baseRows.forEach(row => { baseByT[row.t] = row; });
      return slicerColorsInPrintOrder().map(c => {
        const m = (plan.mapping || []).find(x => x.t === c.t);
        let dst = null;
        if (head) {
          if (m && m.kind && m.kind !== "none") {
            dst = {kind: m.kind, color: c.hex, material: c.material,
                   label: headProposalLabel(m)};
          }
        } else if (m && m.slot) {
          dst = {kind: "slot", color: m.slot.color, material: m.slot.material,
                 label: "ACE " + dispIdx(m.slot.ace) + " / " + dispIdx(m.slot.slot)};
        }
        const base = baseByT[c.t];
        const changed = !!base && ((!!base.dst) !== (!!dst)
          || (base.dst && dst && base.dst.label !== dst.label));
        return {
          t: c.t, src: {hex: c.hex, name: c.name, material: c.material},
          dst, tier: (m && m.tier) || "no_slot",
          options: [], unset: [], editable: false, group: null, changed,
        };
      });
    }
    const colorMapRows = computed(() => {
      const r = preflight.report;
      if (!r) return [];
      const baseKey = r.head_mode ? "loadout" : "slicer";
      if (strategy.value && strategy.value !== "whatif" && strategy.value !== baseKey) {
        return _rowsForPlan(strategy.value);
      }
      if (forcaMixed()) return _rowsForca();
      if (r.head_mode) return _rowsHead();
      return _rowsNormal();
    });
    // One band per FOrca nozzle group, or a single unlabelled band otherwise.
    function cmapBands() {
      const rows = colorMapRows.value;
      if (!rows.length) return [];
      if (!rows[0].group) return [{key: "all", label: "", hint: "", rows}];
      const order = [], byKey = {};
      rows.forEach(row => {
        const k = row.group.key;
        if (!byKey[k]) { byKey[k] = {key: k, label: row.group.label, hint: row.group.hint, rows: []}; order.push(k); }
        byKey[k].rows.push(row);
      });
      return order.map(k => byKey[k]);
    }
    function cmapSummary() {
      const counts = {exact: 0, name: 0, fuzzy: 0, none: 0};
      colorMapRows.value.forEach(row => {
        if (!row.dst) { counts.none++; return; }
        if (row.tier === "exact_hex") counts.exact++;
        else if (String(row.tier || "").indexOf("name") === 0) counts.name++;
        else counts.fuzzy++;
      });
      return t("ui.preflight.cmap_summary", counts);
    }
    function cmapEdited() {
      return Object.keys(preflight.slicerOverrides).length > 0
          || Object.keys(preflight.headOverrides).length > 0;
    }
    function cmapResetAuto() {
      preflight.slicerOverrides = {};
      preflight.slicerSwaps = null;
      preflight.headOverrides = {};
      preflight.headSwaps = null;
    }
    // A single picker for every mode/row: normal-mode rows hand a slotKey
    // to onSlicerSlotChange, head/FOrca rows an id to onHeadTargetChange.
    function cmapPick(row, id) {
      const r = preflight.report;
      if (r && r.head_mode) onHeadTargetChange(row.t, id);
      else onSlicerSlotChange(row.t, id);
    }
    // =================================================================
    // Loadout strategies (redesign plan section 6): one tab strip + one
    // panel replaces the three/four stacked plan blocks. Plan KEYS are
    // untouched (startPreflightPrint/applyLoadout/_rewritePayload keep
    // their arguments) - only how they are presented changes.
    // =================================================================
    const strategy = ref("");   // "" = pick the default once a report lands
    function strategyTabs() {
      const r = preflight.report;
      if (!r) return [];
      const baseKey = r.head_mode ? "loadout" : "slicer";
      // Optimize/layer/colour were never offered under mixed nozzles (the
      // nozzle-blind optimizer does not know about the diameter gate) - the
      // FOrca branch always printed from the base plan only, and that
      // invariant carries over unchanged.
      const keys = forcaMixed() ? [baseKey]
                 : r.head_mode ? ["loadout", "optimize", "layer", "color"]
                               : ["slicer", "optimize", "layer"];
      const base = r.plans[baseKey];
      const tabs = keys.filter(k => r.plans[k]).map(k => ({
        key: k,
        label: t("ui.preflight.strat_" + (k === "slicer" ? "loadout" : k)),
        feasible: r.head_mode ? headPlanFeasible(k) : !!r.plans[k].feasible,
        reason: (r.plans[k] && r.plans[k].reason) || "",
        swaps: r.head_mode ? headPlanSwaps(k)
                           : (k === "slicer" ? slicerSwapsDisplay() : r.plans[k].swaps),
        delta: _planDelta(r.plans[k], base),
      }));
      tabs.push({key: "whatif", label: t("ui.preflight.strat_whatif"),
                feasible: true, swaps: null, delta: ""});
      return tabs;
    }
    // vs. the AS-LOADED plan's own estimate - a different number from
    // estimateDelta (which compares against the slicer's own estimate and
    // stays inside the card).
    function _planDelta(plan, base) {
      if (!plan || !plan.estimate || !base || !base.estimate) return "";
      const d = Number(plan.estimate.total_s) - Number(base.estimate.total_s);
      if (!d) return "";
      return (d > 0 ? "+" : "-") + fmtDuration(Math.abs(d));
    }
    function selectedPlan() {
      const r = preflight.report;
      if (!r || !strategy.value || strategy.value === "whatif") return null;
      return r.plans[strategy.value] || null;
    }
    // "To use this layout, move:" - the physical instruction the mapping
    // table only implied today. Diffs _loadoutOps' overrides against the
    // CURRENT live loadout.
    function loadoutMoves() {
      const r = preflight.report;
      if (!r || !strategy.value || strategy.value === "whatif") return [];
      const baseKey = r.head_mode ? "loadout" : "slicer";
      if (strategy.value === baseKey) return [];
      const ops = r.head_mode ? _loadoutOps("head", strategy.value)
                              : _loadoutOps(strategy.value);
      const moves = [];
      ops.overrides.forEach(o => {
        const live = (r.live_slots || []).find(s => s.ace === o.ace && s.slot === o.slot);
        const from = live ? (live.material || live.color || t("ui.preflight.cmap_unassigned"))
                          : t("ui.preflight.cmap_unassigned");
        const to = o.material || o.color || "?";
        if (from !== to) moves.push({ace: o.ace, slot: o.slot, from, to});
      });
      return moves;
    }
    function canPrint() {
      const r = preflight.report;
      if (!r || preflight.sending) return false;
      if (!strategy.value || strategy.value === "whatif") return false;
      if (r.missing_materials && r.missing_materials.length) return false;
      if (forcaMixed() && !forcaFeasible()) return false;
      if (r.head_mode) return headPlanFeasible(strategy.value);
      const plan = r.plans[strategy.value];
      return !!(plan && plan.feasible);
    }
    function canApplyLoadout() {
      const r = preflight.report;
      if (!r || preflight.applying || preflight.sending) return false;
      if (!strategy.value || strategy.value === "whatif") return false;
      const baseKey = r.head_mode ? "loadout" : "slicer";
      if (strategy.value === baseKey) return false;   // nothing to apply
      return r.head_mode ? headPlanFeasible(strategy.value)
                         : !!(r.plans[strategy.value] && r.plans[strategy.value].feasible);
    }
    function printSelected() {
      const r = preflight.report;
      if (!r) return;
      r.head_mode ? startPreflightPrint("head", strategy.value)
                 : startPreflightPrint(strategy.value);
    }
    // Same plan, same feasibility gate as Print - just uploaded with
    // print=false into the queue instead of started immediately.
    function stageSelected() {
      const r = preflight.report;
      if (!r) return;
      r.head_mode ? startPreflightPrint("head", strategy.value, true)
                 : startPreflightPrint(strategy.value, undefined, true);
    }
    function applySelected() {
      const r = preflight.report;
      if (!r) return;
      r.head_mode ? applyLoadout("head", strategy.value)
                 : applyLoadout(strategy.value);
    }
    // Default tab on a new report: as-loaded, unless it is infeasible and
    // another tab is not - the dialog must never open on a dead end.
    watch(() => preflight.report, (r) => {
      if (!r) { strategy.value = ""; return; }
      const baseKey = r.head_mode ? "loadout" : "slicer";
      const baseFeasible = r.head_mode ? headPlanFeasible(baseKey)
                                       : !!(r.plans[baseKey] && r.plans[baseKey].feasible);
      if (baseFeasible) { strategy.value = baseKey; return; }
      const firstOk = strategyTabs().find(tb => tb.key !== "whatif" && tb.feasible);
      strategy.value = firstOk ? firstOk.key : baseKey;
    });
    // ---- Send-to-multiACE inbox -----------------------------------------
    // A slicer pushed a raw gcode to /api/preflight/inbox (store-only). The
    // pickup fetches it INTO THE BROWSER and runs the normal Pyodide
    // preflight - analysis on this PC, against the slot state of right now,
    // never on the U1's CPU. One-shot delivery: the inbox is cleared at
    // pickup, so cancelling the preview means re-sending from the slicer.
    // Auto-open fires once per delivery and only while the printer is not
    // printing; a delivery during a print waits as a banner.
    let _inboxAutoTried = "";
    const inboxBusy = ref(false);
    function _inboxKey() {
      const ib = state.preflight_inbox || {};
      return ib.pending ? (ib.ts + ":" + (ib.name || "")) : "";
    }
    const inboxCanStart = computed(() => {
      const ps = state.printer_state;
      return !!(state.preflight_inbox && state.preflight_inbox.pending)
        && !inboxBusy.value
        && ps !== 'printing' && ps !== 'paused' && ps !== 'busy'
        && !state.toolheads.some(th => th.manual);
    });
    function _maybeAutoOpenInbox() {
      if (panelMode) return;
      if (!inboxCanStart.value) return;
      if (preflight.open || preflight.busy) return;
      const key = _inboxKey();
      if (!key || key === _inboxAutoTried) return;
      _inboxAutoTried = key;
      startInboxPreflight();
    }
    async function startInboxPreflight() {
      if (inboxBusy.value) return;
      const ib = state.preflight_inbox;
      if (!ib || !ib.pending) return;
      inboxBusy.value = true;
      try {
        const r = await fetch(`${API}/preflight/inbox/file`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        const f = new File([blob], ib.name || "upload.gcode",
                           {type: "text/plain"});
        await fetch(`${API}/preflight/inbox`, {method: "DELETE"});
        state.preflight_inbox = {pending: false, name: null, size: 0, ts: 0};
        _runPreflight(f);
      } catch (e) {
        setMacroLog(t('ui.inbox.fetch_failed', {error: String(e)}));
      } finally {
        inboxBusy.value = false;
      }
    }
    function dismissInbox() {
      fetch(`${API}/preflight/inbox`, {method: "DELETE"}).catch(() => {});
      state.preflight_inbox = {pending: false, name: null, size: 0, ts: 0};
    }
    function onUploadGcode(fileList) {
      const f = fileList && fileList[0];
      if (uploadInput.value) uploadInput.value.value = "";
      if (!f) return;
      const lower = f.name.toLowerCase();
      if (!(lower.endsWith(".gcode") || lower.endsWith(".gco") || lower.endsWith(".g"))) {
        confirm({
          title: t("ui.upload.title"),
          message: t("ui.upload.bad_ext"),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
        return;
      }
      // Preflight can't handle a manual/TPU head (hand-fed, no ACE slot) - it
      // would be ignored/mis-assigned. Disable preflight while one is active;
      // the user uploads directly via Fluidd instead. (Full support is Pro.)
      if (state.toolheads.some(th => th.manual)) {
        confirm({
          title: t("ui.upload.title"),
          message: t("ui.preflight.manual_disabled"),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
        return;
      }
      _runPreflight(f);
    }
    // ---- in-browser (Pyodide) preflight ----------------------------------
    // The heavy parse/rewrite runs in a Web Worker via Pyodide, executing the
    // UNMODIFIED post-processor + preflight_core (served by /api/preflight/pysrc)
    // - the same Python the backend runs, so no JS re-port / drift. Falls back
    // to the server /api/preflight path if the browser can't do it (no Worker,
    // offline CDN, etc.).
    const PYODIDE_INDEX_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/";
    let preflightWorker = null;
    let preflightWorkerReady = null;
    let preflightFile = null;
    let preflightJobId = "";
    let preflightJobSeq = 0;

    function ensurePreflightWorker() {
      if (!window.Worker) throw new Error(t("ui.preflight.local_worker_missing"));
      if (preflightWorker && preflightWorkerReady) return preflightWorkerReady;
      preflightWorker = new Worker("preflight_pyodide_worker.js?v=pyodide-20260902-stream");
      preflightWorkerReady = (async () => {
        const r = await fetch(`${API}/preflight/pysrc`);
        if (!r.ok) throw new Error("pysrc " + r.status);
        const src = await r.json();
        // Single source of truth for the size cap: the backend serves
        // MULTIACE_PREFLIGHT_MAX_MB here so raising it moves both the
        // server's cap and the browser's cutoff - instead of the browser
        // keeping an independently hardcoded copy that silently drifts
        // from the backend's.
        const maxMb = Number(src.max_upload_mb);
        if (maxMb > 0) preflightLocalMaxBytes = maxMb * 1024 * 1024;
        await new Promise((resolve, reject) => {
          const onMsg = (ev) => {
            const m = ev.data || {};
            if (m.type === "ready") {
              preflightWorker.removeEventListener("message", onMsg); resolve();
            } else if (m.type === "error") {
              preflightWorker.removeEventListener("message", onMsg);
              reject(new Error(m.message || "worker init failed"));
            }
          };
          preflightWorker.addEventListener("message", onMsg);
          preflightWorker.postMessage({
            type: "init",
            pyodideIndexURL: PYODIDE_INDEX_URL,
            postprocessSrc: src.postprocess,
            coreSrc: src.core,
            // Optional third module: without it the preflight still works,
            // it just carries no time/filament estimate.
            swapCostSrc: src.swap_cost || null,
            costParams: src.cost_params || null,
            calibration: src.calibration || null,
          });
        });
      })();
      // On a failed bring-up, drop the worker so the next attempt re-inits.
      preflightWorkerReady.catch(() => {
        try { preflightWorker.terminate(); } catch (_) {}
        preflightWorker = null; preflightWorkerReady = null;
      });
      return preflightWorkerReady;
    }

    function runPreflightWorker(type, payload, onProgress) {
      return new Promise((resolve, reject) => {
        const worker = preflightWorker;
        const jobId = payload.jobId;
        // "rewrite" streams its output back as a series of Transferable
        // ArrayBuffer chunks (see preflight_pyodide_worker.js) instead of
        // one whole-file string, so it never has to be materialized as a
        // single JS string here either - the chunks are handed straight to
        // `new Blob([...chunks])` by the caller.
        const chunks = [];
        const onMsg = (ev) => {
          const msg = ev.data || {};
          if (msg.jobId && msg.jobId !== jobId) return;
          if (msg.type === "progress") { if (onProgress) onProgress(msg); return; }
          if (msg.type === "rewrite-chunk") { chunks.push(msg.chunk); return; }
          if (msg.type === "error") {
            worker.removeEventListener("message", onMsg);
            reject(new Error(msg.message || "worker error"));
            return;
          }
          if ((type === "analyze" && msg.type === "analyze-done")
              || (type === "rewrite" && msg.type === "rewrite-done")
              || (type === "estimate" && msg.type === "estimate-done")) {
            worker.removeEventListener("message", onMsg);
            if (type === "rewrite") msg.chunks = chunks;
            resolve(msg);
          }
        };
        worker.addEventListener("message", onMsg);
        worker.postMessage(Object.assign({type}, payload));
      });
    }

    // Live ACE/slot identity + head-mode context, in the exact shape
    // preflight_core expects. Fetched from the backend (single source) rather
    // than re-derived in JS.
    async function loadLiveSlotsForPreflight() {
      const r = await fetch(`${API}/preflight/livedata`);
      if (!r.ok) {
        let d = `${r.status}`;
        try { const j = await r.json(); if (j.detail) d = j.detail; } catch (_) {}
        throw new Error(d);
      }
      return await r.json();   // {live_slots, head_ctx}
    }

    function clearLocalPreflightJob() {
      if (preflightWorker && preflightJobId) {
        try { preflightWorker.postMessage({type: "clear", jobId: preflightJobId}); } catch (_) {}
      }
      preflightJobId = "";
      preflightFile = null;
    }

    // A refusal by preflight_core.PreflightRejected is about the FILE, so it
    // reads as a sentence, not as a Python traceback - and it must not offer
    // the in-printer fallback, which runs the same check and refuses too.
    // Pyodide hands us the whole traceback, so pull the final line out of it.
    function _preflightRejection(msg) {
      const m = /PreflightRejected:\s*([^\n]+)/.exec(msg || "");
      return m ? m[1].trim() : null;
    }
    // Pyodide's ingestion/output paths now stream through MEMFS in chunks
    // rather than materializing the whole file as a JS string (see
    // preflight_pyodide_worker.js), which is what makes it survivable at
    // the size this cap allows. It still shares ONE budget with the
    // printer-side cap (main.py's _PREFLIGHT_MAX_SIZE /
    // MULTIACE_PREFLIGHT_MAX_MB) - fetched from /api/preflight/pysrc's
    // max_upload_mb in ensurePreflightWorker() and written into this `let`.
    // The literal below is only the pre-fetch fallback for the very first
    // file dropped in a fresh tab, before that fetch has resolved.
    let preflightLocalMaxBytes = 500 * 1024 * 1024;
    // Entry point: try the browser path, offer the server fallback on failure.
    async function _runPreflight(f) {
      // Opened here (not inside _runLocalPreflight/_runServerPreflight) so
      // the dialog - and the canvas the preview renders into - exists before
      // the parse worker is fired. Two separate workers, so this runs in
      // parallel with the analysis below; never awaited, so a slow or
      // failed parse can never delay or block the preflight itself.
      preflight.open = true;
      startGcodePreview(f);
      if (f.size > preflightLocalMaxBytes) {
        // Skip the browser path entirely: attempting it here has crashed the
        // tab outright on real files (Pyodide OOM), so there is no local
        // error to catch and fall back from - go straight to the printer,
        // which enforces its own size cap and explains the slicer
        // post-processing alternative.
        await _runServerPreflight(f);
        return;
      }
      try {
        await _runLocalPreflight(f);
      } catch (e) {
        const msg = e && e.message ? e.message : String(e);
        const rejected = _preflightRejection(msg);
        if (rejected) {
          confirm({
            title: t("ui.preflight.rejected_title"),
            message: rejected,
            okLabel: t("ui.common.ok"),
            dismissOnly: true,      // one button: there is nothing to choose
            onOk: () => { closePreflight(); },
          });
          return;
        }
        confirm({
          title: t("ui.preflight.local_failed_title"),
          message: t("ui.preflight.local_failed_msg", {error: msg}),
          okLabel: t("ui.preflight.local_fallback_ok"),
          // No altLabel: the dialog already renders its own Cancel, and
          // passing one labelled "Cancel" produced TWO identical buttons.
          onCancel: () => { closePreflight(); },
          onOk: () => { _runServerPreflight(f); },
        });
      }
    }
    // What-if (plan section 6.3): re-run the whole preflight against the
    // edited virtual spools. preflightFile is a closure-local, not exposed
    // to the template, so the button in the dialog calls this wrapper.
    function whatifRerun() {
      if (preflightFile) _runPreflight(preflightFile);
    }

    async function _runLocalPreflight(f) {
      preflight.open    = true;
      preflight.busy    = true;
      preflight.sending = "";
      preflight.report  = null;
      preflight.error   = "";
      preflight.local   = true;
      preflight.progress = {percent: 0, stage: "queued", running: true};
      uploading.value   = true;
      clearLocalPreflightJob();
      preflightFile  = f;
      preflightJobId = `local-${Date.now()}-${++preflightJobSeq}`;
      try {
        await ensurePreflightWorker();
        const live = await loadLiveSlotsForPreflight();
        // Analysis only. _startLocalPreflightPrint re-fetches the live
        // loadout before the rewrite, so an overlay cannot reach the file
        // that gets printed.
        const vp = virtualPayload();
        const j = await runPreflightWorker("analyze", {
          jobId: preflightJobId, file: f,
          liveSlots: vp || live.live_slots, headCtx: live.head_ctx,
          spoolPrices: live.spool_prices,
          // Fresh every run (not just at worker init) so a swap_retract_length
          // / swap_purge_length edit in Settings shows up without reloading
          // the page - the worker itself outlives many preflight runs.
          costParams: live.cost_params, calibration: live.calibration,
        }, msg => {
          preflight.progress = {
            percent: Number(msg.percent || 0),
            stage:   String(msg.stage || ""), running: true};
        });
        preflight.report = Object.assign(
          {local: true, virtual_loadout: !!vp}, j.report || {});
        preflight.slicerOverrides = {};
        preflight.slicerSwaps = null;
        preflight.headOverrides = {};
        preflight.headSwaps = null;
      } finally {
        uploading.value = false;
        preflight.busy  = false;
        // In the finally, not at the end of the try: on an exception the bar
        // kept its last value with running:true and sat there frozen behind
        // the dialog (seen on the re-processing refusal).
        preflight.progress = null;
      }
    }

    async function _runServerPreflight(f) {
      preflight.open    = true;
      preflight.busy    = true;
      preflight.sending = "";
      preflight.report  = null;
      preflight.error   = "";
      preflight.local   = false;
      preflight.progress = {percent: 0, stage: "queued", running: true};
      uploading.value   = true;
      // Kept for the g-code preview, which needs nothing but this File
      // object and runs identically on the server-preflight path.
      preflightFile = f;
      const FIRST_POLL_MS = 250;
      const POLL_MS       = 500;
      try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const vp = virtualPayload();
        if (vp) fd.append("virtual_slots", JSON.stringify(vp));
        // The streamed-upload + async job/percent path (main.py's
        // _run_preflight_analyze_job) - gives this a real, moving progress
        // bar instead of one blocking POST with nothing to show while a
        // large file is analysed.
        const r = await fetch(`${API}/preflight/analyze`, {method: "POST", body: fd});
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        const {job_id: jobId} = await r.json();
        let last;
        let pollDelay = FIRST_POLL_MS;
        for (;;) {
          await new Promise(res => setTimeout(res, pollDelay));
          pollDelay = POLL_MS;
          let sr;
          try {
            sr = await fetch(`${API}/preflight/analyze/status?job_id=${encodeURIComponent(jobId)}`);
          } catch (_) {
            continue;
          }
          if (!sr.ok) {
            const sj = await sr.json().catch(() => ({}));
            throw new Error(sj.detail || `${sr.status} ${sr.statusText}`);
          }
          last = await sr.json();
          preflight.progress = {
            percent: Number(last.percent || 0),
            stage:   String(last.stage || ""),
            running: !last.done,
          };
          if (last.done) break;
        }
        if (last.error) throw new Error(last.error);
        preflight.report = last.report;
        preflight.slicerOverrides = {};
        preflight.slicerSwaps = null;
        preflight.headOverrides = {};
        preflight.headSwaps = null;
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        uploading.value = false;
        preflight.busy  = false;
        preflight.progress = null;
      }
    }
    // =================================================================
    // Virtual loadout (plan section 2.2)
    //
    // A what-if overlay: "would 2 ACEs beat 1?", "what if green sat in ACE
    // 1 instead?". It is READ-ONLY with respect to the printer - it can
    // never write slot state, and it lives in its own field all the way to
    // the backend, because the rewrite path reads live_slots and only
    // live_slots. A virtual loadout that leaked into a real rewrite would
    // swap to spools holding a different material and run them at the
    // wrong temperature.
    // =================================================================
    // Backend-persisted (GET/PUT /api/virtual-loadout) so it survives a
    // different browser/device or a cleared profile - not just this one's
    // localStorage. The localStorage copy stays as an instant local cache:
    // it paints the last-known state before the initial fetch resolves, and
    // is the fallback if that fetch fails (offline / backend restarting).
    const VIRTUAL_KEY = "multiace.virtualLoadout";
    const virtualLoadout = reactive({
      enabled: false,
      slots: [],          // [{ace, slot, material, color}]
    });
    let _virtualLoadoutSaveTimer = null;
    let _virtualLoadoutLoaded = false;

    function _applyVirtualLoadout(j) {
      virtualLoadout.enabled = !!j.enabled;
      virtualLoadout.slots = Array.isArray(j.slots) ? j.slots : [];
    }
    function loadVirtualLoadoutCache() {
      try {
        const raw = localStorage.getItem(VIRTUAL_KEY);
        if (raw) _applyVirtualLoadout(JSON.parse(raw));
      } catch (_) {}
    }
    async function loadVirtualLoadout() {
      loadVirtualLoadoutCache();
      try {
        const r = await fetch(`${API}/virtual-loadout`);
        if (r.ok) _applyVirtualLoadout(await r.json());
      } catch (_) {
        // Backend unreachable - keep the localStorage cache loaded above.
      } finally {
        _virtualLoadoutLoaded = true;
      }
    }
    function saveVirtualLoadout() {
      const payload = {enabled: virtualLoadout.enabled, slots: virtualLoadout.slots};
      try { localStorage.setItem(VIRTUAL_KEY, JSON.stringify(payload)); } catch (_) {}
      // Skip the round-trip while the initial GET is still in flight, so it
      // can't win a race and clobber a fetch that resolves after it.
      if (!_virtualLoadoutLoaded) return;
      clearTimeout(_virtualLoadoutSaveTimer);
      _virtualLoadoutSaveTimer = setTimeout(() => {
        fetch(`${API}/virtual-loadout`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        }).catch(() => {});
      }, 400);
    }
    watch(() => JSON.stringify(virtualLoadout), saveVirtualLoadout);
    loadVirtualLoadout();

    function virtualSeed(aceCount) {
      // Start from what is actually in the machine, so the first edit is a
      // change to reality rather than a blank slate.
      const rows = [];
      for (const a of state.aces.slice(0, aceCount)) {
        for (const s of a.slots || []) {
          rows.push({
            ace: a.idx, slot: s.idx,
            material: s.material || "",
            color: _hex6(s.color) || "#888888",
          });
        }
      }
      // Pad out any ACE the printer does not have, so "would 2 beat 1?" is
      // answerable on a machine with one.
      for (let a = state.aces.length; a < aceCount; a++) {
        for (let s = 0; s < 4; s++) {
          rows.push({ace: a, slot: s, material: "PLA", color: "#888888"});
        }
      }
      virtualLoadout.slots = rows;
    }
    function virtualAceCount() {
      return new Set(virtualLoadout.slots.map(r => r.ace)).size;
    }
    function virtualExport() {
      const url = URL.createObjectURL(new Blob(
        [JSON.stringify(virtualLoadout.slots, null, 2)],
        {type: "application/json"}));
      const a = document.createElement("a");
      a.href = url;
      a.download = "multiace-virtual-loadout.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    }
    // The payload sent alongside the upload. Empty unless the user turned
    // the overlay on AND filled it in - so the default path is untouched.
    function virtualPayload() {
      if (!virtualLoadout.enabled || !virtualLoadout.slots.length) return null;
      return virtualLoadout.slots
        .filter(r => r.material || r.color)
        .map(r => ({ace: Number(r.ace), slot: Number(r.slot),
                    material: r.material || "", color: _hex6(r.color)}));
    }

    // =================================================================
    // G-code preview (plan section 9)
    //
    // Parses and renders entirely in the browser, off the file already
    // selected. No upload, no Moonraker, no printer required - the earlier
    // design uploaded the file into the printer's own gcodes folder and
    // embedded that printer's web UI in an iframe, which needs a real
    // printer behind it and renders nothing without one. This needs
    // nothing but the File object already sitting in memory, so it works
    // identically on a laptop with no printer attached, in mock mode, or
    // against a live printer.
    //
    // Colours by the SLICER's tool index (the file's own colours), so it
    // answers "does this file look right at all" - not "what will
    // multiACE's plan actually print", which the swim-lane timeline above
    // already answers without needing any geometry.
    // =================================================================
    const GPREVIEW_SPEEDS = [1, 5, 10, 100];
    // This preview worker still reads the whole file via file.text() (its
    // own, separate memory cost - a JS string ~2x the file's byte size,
    // plus its typed-array segment budget), and _runPreflight() starts it
    // UNCONDITIONALLY in parallel with the preflight worker's own analyze.
    // Now that preflight can legitimately be handling a file up to
    // preflightLocalMaxBytes (shared config, default 500MB) in the other
    // worker at the same time, this auto-start guard is kept well below
    // that ceiling on purpose, so the two don't compound peak memory on
    // exactly the large files this is meant to make safer. Above this size
    // the box just shows its manual "Load preview" affordance instead -
    // fully streaming this worker's own ingestion (matching what the
    // preflight worker now does) is a reasonable follow-up but out of
    // scope here.
    const GPREVIEW_AUTO_MAX_BYTES = 150 * 1024 * 1024;
    const gpreview = reactive({
      // idle: dialog open, parse not started yet. parsing: worker running.
      // ready: renderer live. error: parse threw. skipped: too large / opted
      // out - the box still shows a [Load preview] affordance.
      phase: "idle", error: "", progress: 0,
      collapsed: localStorage.getItem("multiace.gpreview.collapsed") === "1",
      // A layer RANGE, not a single layer: a range is what shows where a
      // colour starts and stops, which is the question this app exists to
      // answer. layerLo/layerHi are inclusive.
      layerLo: 0, layerHi: 0, totalLayers: 0,
      // Sequential-move scrub within the TOP visible layer.
      move: 0, moveMax: 0, playing: false,
      speed: GPREVIEW_SPEEDS.includes(Number(localStorage.getItem("multiace.gpreview.speed")))
        ? Number(localStorage.getItem("multiace.gpreview.speed")) : 1,
      viewType: "filament", lod: "auto",
      showTravel: false, showToolchanges: true, showPlate: true,
      maximized: false,
      // "gl" or "2d" - the UI says which, rather than letting the two
      // pretend to be the same thing.
      mode: "gl", segments: 0, decimated: null, roles: [],
      isolate: null,
    });
    const gpreviewCanvasEl = ref(null);
    let gpreviewData = null;
    let gpreviewRenderer = null;
    let gpreviewResizeObs = null;
    let gpreviewRaf = 0;

    // Distinguishable colours for the toolchange legend BEFORE the analysis
    // report lands (the parse usually finishes first) - without this every
    // tool falls back to the same grey and the preview shows an
    // undifferentiated part while the user is staring right at it.
    const GPREVIEW_FALLBACK_PALETTE = [
      "#e6553a", "#3a8ee6", "#3ae67a", "#e6c73a",
      "#a53ae6", "#e63aa0", "#3ae6d8", "#e6923a",
    ];
    function _gpreviewColorForT(t) {
      const rep = preflight.report;
      const c = rep && (rep.slicer_colors || []).find(x => x.t === t);
      if (c && c.hex) return c.hex;
      const n = GPREVIEW_FALLBACK_PALETTE.length;
      return GPREVIEW_FALLBACK_PALETTE[((Number(t) % n) + n) % n];
    }

    // ---- the multiACE twist (plan 7.5) ---------------------------------
    // OrcaSlicer colours "Tool" by extruder index because a slicer knows
    // nothing about the hardware. multiACE knows which physical ACE unit
    // and slot feeds each tool, and knows from the preflight report
    // whether the current loadout can actually satisfy the print. So the
    // legend names the HARDWARE, and the toolchange markers carry
    // FEASIBILITY - the preview shows where the print will fail, before it
    // starts.
    function _gpreviewTarget(t) {
      const rep = preflight.report;
      if (!rep) return null;
      if (rep.head_mode) {
        const id = headEffectiveTargetId(t);
        return id ? (headTargets().find(x => x.id === id) || null) : null;
      }
      return slicerEffectiveSlot(t) || null;
    }
    function gpreviewFeasible(t) {
      return !!_gpreviewTarget(t);
    }
    const gpreviewLegend = computed(() => {
      if (!gpreviewData) return [];
      const seen = new Set();
      for (let i = 0; i < gpreviewData.segmentCount; i++) seen.add(gpreviewData.tool[i]);
      return [...seen].sort((a, b) => a - b).map(tt => {
        const tg = _gpreviewTarget(tt);
        const bits = ["T" + dispIdx(tt)];
        if (tg && tg.kind === "feeder") {
          bits.push(t("ui.preflight.feeder"));
        } else if (tg && tg.ace !== null && tg.ace !== undefined) {
          bits.push(`ACE ${dispIdx(tg.ace)} / ${t("ui.gpreview.slot")} ${dispIdx(tg.slot)}`);
        }
        const mat = (tg && tg.material) || "";
        if (mat) bits.push(mat);
        return {
          t: tt,
          color: _gpreviewColorForT(tt),
          label: bits.join(" · "),
          feasible: !!tg,
        };
      });
    });
    // Feature-view legend: only the roles this file actually contains.
    const gpreviewFeatureLegend = computed(() => {
      if (!gpreviewData || gpreview.viewType !== "feature") return [];
      const seen = new Set();
      for (let i = 0; i < gpreviewData.segmentCount; i++) seen.add(gpreviewData.role[i]);
      return [...seen].sort((a, b) => a - b).map(r => ({
        r,
        color: MultiAceGcodePreview.ROLE_COLORS[r] || "#e6e6e6",
        label: (gpreviewData.roles || [])[r] || "?",
      }));
    });

    function _gpreviewScheduleDraw() {
      if (gpreviewRaf) return;
      gpreviewRaf = requestAnimationFrame(() => {
        gpreviewRaf = 0;
        if (gpreviewRenderer) gpreviewRenderer.draw();
      });
    }
    function _gpreviewApply() {
      const r = gpreviewRenderer;
      if (!r) return;
      r.setLayerRange(gpreview.layerLo, gpreview.layerHi);
      gpreview.moveMax = r.topLayerMoveCount();
      if (gpreview.move > gpreview.moveMax) gpreview.move = gpreview.moveMax;
      r.setMoveLimit(gpreview.move >= gpreview.moveMax ? null : gpreview.move);
      r.setViewType(gpreview.viewType);
      r.setLod(gpreview.lod);
      r.setShowTravel(gpreview.showTravel);
      r.setShowToolchanges(gpreview.showToolchanges);
      r.setShowPlate(gpreview.showPlate);
      r.setIsolateTool(gpreview.isolate);
      gpreview.segments = r.visibleSegmentCount();
      _gpreviewScheduleDraw();
    }
    watch(() => [gpreview.layerLo, gpreview.layerHi, gpreview.move,
                 gpreview.viewType, gpreview.lod, gpreview.showTravel,
                 gpreview.showToolchanges, gpreview.showPlate,
                 gpreview.isolate],
          _gpreviewApply);

    // The parse usually finishes before the analysis does, so the preview
    // starts with fallback colours; once the report (or a manual override)
    // changes what feeds what, repaint without re-uploading geometry -
    // feasibility (the red toolchange markers) is derived from the
    // overrides too, so a pick in the colour map below must repaint the
    // preview above.
    watch(() => [preflight.report, preflight.slicerOverrides, preflight.headOverrides],
          () => {
      if (!gpreviewRenderer) return;
      gpreviewRenderer.setColorForT(_gpreviewColorForT);
      gpreviewRenderer.setToolchangeColor(
        (tc) => gpreviewFeasible(tc.t) ? _gpreviewColorForT(tc.t) : "#ff4d4d");
      _gpreviewScheduleDraw();
    }, {deep: true});

    function gpreviewSetLayerLo(v) {
      gpreview.layerLo = Math.min(Number(v), gpreview.layerHi);
    }
    function gpreviewSetLayerHi(v) {
      gpreview.layerHi = Math.max(Number(v), gpreview.layerLo);
    }
    function gpreviewPreset(name) {
      if (!gpreviewRenderer) return;
      gpreviewRenderer.setPreset(name);
      _gpreviewScheduleDraw();
    }
    function gpreviewIsolate(t) {
      gpreview.isolate = (t === null || t === undefined) ? null : t;
    }
    function gpreviewToggleMax() {
      gpreview.maximized = !gpreview.maximized;
      nextTick(() => {
        if (!gpreviewRenderer) return;
        gpreviewRenderer.resize();
        _gpreviewScheduleDraw();
      });
    }
    // The effective LOD, for the readout - `auto` has to say what it
    // actually chose, or the control is a mystery box.
    const gpreviewLod = computed(() => {
      void gpreview.segments;   // recompute when the visible band changes
      return gpreviewRenderer ? gpreviewRenderer.effectiveLod() : "ribbon";
    });

    // Auto-start only below the size guard (the parse allocates several
    // typed arrays per segment and would otherwise peak alongside Pyodide's
    // heap); above it, or when the user opted out, the box shows a manual
    // [Load preview] affordance instead. `opts.manual` bypasses both guards
    // (Retry / Load preview button).
    function _gpreviewShouldAutoStart(file) {
      if (panelMode) return false;
      if (localStorage.getItem("multiace.gpreview.auto") === "0") return false;
      if (file && file.size > GPREVIEW_AUTO_MAX_BYTES) return false;
      return true;
    }
    async function startGcodePreview(file, opts) {
      opts = opts || {};
      // The embedded Fluidd panel never shows this dialog at all, so it
      // must never spend CPU/memory parsing a file it cannot render.
      if (panelMode) { gpreview.phase = "skipped"; return; }
      file = file || preflightFile;
      if (!file || gpreview.phase === "parsing") return;
      if (!opts.manual && !_gpreviewShouldAutoStart(file)) {
        gpreview.phase = "skipped";
        return;
      }
      gpreview.phase = "parsing";
      gpreview.error = "";
      gpreview.progress = 0;
      try {
        gpreviewData = await MultiAceGcodePreview.parse(file, {
          onProgress: (pct) => { gpreview.progress = pct; },
        });
        if (!gpreviewData.segmentCount) {
          throw new Error(t("ui.gpreview.no_extrusion"));
        }
        gpreview.totalLayers = gpreviewData.layerCount;
        gpreview.roles = gpreviewData.roles || [];
        gpreview.decimated = gpreviewData.decimated;
        // Start on the whole print rather than layer 0 - the first thing
        // worth seeing is "does the finished object look right".
        gpreview.layerLo = 0;
        gpreview.layerHi = Math.max(0, gpreview.totalLayers - 1);
        await nextTick();
        // The dialog may have been closed while the parse was running.
        if (!gpreviewCanvasEl.value) return;
        gpreviewRenderer = new MultiAceGcodePreview.Renderer(gpreviewCanvasEl.value);
        gpreview.mode = gpreviewRenderer.mode;
        gpreviewRenderer.setData(gpreviewData, _gpreviewColorForT);
        gpreviewRenderer.setToolchangeColor(
          (tc) => gpreviewFeasible(tc.t) ? _gpreviewColorForT(tc.t) : "#ff4d4d");
        gpreviewRenderer.onDraw(_gpreviewScheduleDraw);
        gpreviewRenderer.resize();
        gpreview.move = gpreviewRenderer.topLayerMoveCount();
        gpreview.phase = "ready";
        _gpreviewApply();
        if (!gpreviewResizeObs && window.ResizeObserver) {
          gpreviewResizeObs = new ResizeObserver(() => {
            if (!gpreviewRenderer) return;
            gpreviewRenderer.resize();
            _gpreviewScheduleDraw();
          });
          gpreviewResizeObs.observe(gpreviewCanvasEl.value);
        }
      } catch (e) {
        gpreview.error = e.message || String(e);
        gpreview.phase = "error";
      }
    }

    function gpreviewToggleCollapse() {
      gpreview.collapsed = !gpreview.collapsed;
      try {
        localStorage.setItem("multiace.gpreview.collapsed", gpreview.collapsed ? "1" : "0");
      } catch (_) {}
      if (gpreview.collapsed && gpreview.playing) gpreviewTogglePlay();
    }
    const gpreviewAutoEnabled = ref(localStorage.getItem("multiace.gpreview.auto") !== "0");
    function gpreviewSetAutoStart(enabled) {
      gpreviewAutoEnabled.value = enabled;
      try { localStorage.setItem("multiace.gpreview.auto", enabled ? "1" : "0"); } catch (_) {}
    }

    function closeGcodePreview() {
      gpreview.phase = "idle";
      gpreview.playing = false;
      gpreview.maximized = false;
      gpreview.isolate = null;
      if (gpreviewPlayRaf) { cancelAnimationFrame(gpreviewPlayRaf); gpreviewPlayRaf = 0; }
      if (gpreviewRaf) { cancelAnimationFrame(gpreviewRaf); gpreviewRaf = 0; }
      if (gpreviewResizeObs) { gpreviewResizeObs.disconnect(); gpreviewResizeObs = null; }
      if (gpreviewRenderer) gpreviewRenderer.dispose();
      gpreviewRenderer = null;
      gpreviewData = null;
    }

    // Play drives the MOVE slider, and rolls onto the next layer when it
    // runs off the end of this one - which is the print building itself,
    // rather than a layer flicker book. A rAF loop (not setInterval) so the
    // speed can change mid-play without restarting or jumping, and so
    // playback actually stops in a background tab instead of racing ahead
    // invisibly.
    const GPREVIEW_LAYER_SECONDS = 3.5;      // one layer at 1x
    let gpreviewPlayRaf = 0;
    let gpreviewPlayLast = 0;
    let gpreviewMoveAcc = 0;                 // fractional moves carry over

    function gpreviewSetSpeed(s) {
      gpreview.speed = GPREVIEW_SPEEDS.includes(s) ? s : 1;
      try { localStorage.setItem("multiace.gpreview.speed", String(gpreview.speed)); }
      catch (_) {}
    }
    function _gpreviewPlayStep(now) {
      if (!gpreview.playing) return;
      const dt = Math.min(0.25, (now - gpreviewPlayLast) / 1000);   // tab-switch clamp
      gpreviewPlayLast = now;
      gpreviewMoveAcc += (gpreview.moveMax / GPREVIEW_LAYER_SECONDS) * gpreview.speed * dt;
      const step = Math.floor(gpreviewMoveAcc);
      if (step >= 1) {
        gpreviewMoveAcc -= step;
        if (gpreview.move + step >= gpreview.moveMax) {
          if (gpreview.layerHi >= gpreview.totalLayers - 1) {
            // last layer fully shown - stop rather than looping back to
            // the start of the range, so play settles on the finished part.
            gpreview.move = gpreview.moveMax;
            gpreview.playing = false;
            gpreviewPlayRaf = 0;
            return;
          }
          // roll onto the next layer - the print building itself, as today
          gpreview.layerHi += 1;
          gpreview.move = 0;
        } else {
          gpreview.move += step;
        }
      }
      gpreviewPlayRaf = requestAnimationFrame(_gpreviewPlayStep);
    }
    function gpreviewTogglePlay() {
      gpreview.playing = !gpreview.playing;
      if (gpreviewPlayRaf) { cancelAnimationFrame(gpreviewPlayRaf); gpreviewPlayRaf = 0; }
      gpreviewMoveAcc = 0;
      if (gpreview.playing) {
        // The default (paused) view sits at the fully-built print - last
        // layer, move at its max. Starting play from exactly that spot
        // would hit the last-layer autostop on the very first tick, so
        // restart from the top of the selected range instead.
        if (gpreview.layerHi >= gpreview.totalLayers - 1 && gpreview.move >= gpreview.moveMax) {
          gpreview.layerHi = gpreview.layerLo;
          gpreview.move = 0;
        }
        gpreviewPlayLast = performance.now();
        gpreviewPlayRaf = requestAnimationFrame(_gpreviewPlayStep);
      }
    }

    function closePreflight() {
      closeGcodePreview();
      preflight.open    = false;
      preflight.report  = null;
      preflight.error   = "";
      preflight.sending = "";
      preflight.progress = null;
      preflight.local   = false;
      strategy.value    = "";
      cmapDetails.value = false;
      clearLocalPreflightJob();
    }
    // ---- estimate formatting (plan 1.4) ---------------------------------
    function fmtDuration(seconds) {
      if (seconds === null || seconds === undefined) return "–";
      const s = Math.max(0, Math.round(Number(seconds)));
      const d = Math.floor(s / 86400);
      const h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (d) return `${d}d ${h}h`;
      if (h) return `${h}h ${m}m`;
      return `${m}m`;
    }
    // The delta against the slicer's own estimate - the number the user is
    // actually asking for ("how much does multiACE cost me?").
    function estimateDelta(est) {
      if (!est || est.base_s === null || est.base_s === undefined) return "";
      if (!est.added_s) return "";
      return "+" + fmtDuration(est.added_s);
    }
    function wastePercent(est) {
      if (!est || !est.totals || !est.totals.g || !est.purge) return 0;
      return Math.round((est.purge.g / est.totals.g) * 1000) / 10;
    }

    // ---- plan timeline (plan section 3.2 / 3.3) --------------------------
    // One swim lane per head, one block per toolchange along the print.
    // The point is that an inline swap and a background swap look
    // different: the first stalls the print, the second does not.
    function swimLanes(plan) {
      const byHead = new Map();
      for (const ev of (plan && plan.timeline) || []) {
        const h = ev.head === null || ev.head === undefined ? -1 : ev.head;
        if (!byHead.has(h)) byHead.set(h, []);
        byHead.get(h).push(ev);
      }
      return [...byHead.keys()].sort((a, b) => a - b)
        .map(head => ({head, events: byHead.get(head)}));
    }
    function swapColor(ev) {
      const rep = preflight.report;
      const c = rep && (rep.slicer_colors || []).find(x => x.t === ev.t);
      return (c && c.hex) || "#666";
    }
    function swapTitle(ev) {
      const secs = Math.round(ev.seconds || 0);
      const win = ev.window_min === null || ev.window_min === undefined
        ? "" : `, window ${ev.window_min} min`;
      const purge = ev.purge_mm ? `, purge ${ev.purge_mm} mm` : "";
      return `T${ev.t} · ${ev.kind} · ${secs} s${win}${purge}\n${ev.note || ""}`;
    }

    function stageLabel(stage) {
      const map = {
        queued:            t("ui.preflight.stage_queued"),
        analyze:           t("ui.preflight.stage_analyze"),
        apply_remap:       t("ui.preflight.stage_apply_remap"),
        optimize:          t("ui.preflight.stage_optimize"),
        layer:             t("ui.preflight.stage_layer"),
        print_prefs:       t("ui.preflight.stage_print_prefs"),
        rewrite:           t("ui.preflight.stage_rewrite"),
        inject_auto_load:  t("ui.preflight.stage_inject_auto_load"),
        upload:            t("ui.preflight.stage_upload"),
        done:              t("ui.preflight.stage_done"),
      };
      return map[stage] || stage || "";
    }
    async function startPreflightPrint(mode, headPlan, stage) {
      if (preflight.busy || preflight.sending) return;
      const rep = preflight.report;
      if (!rep) return;
      if (rep.local) { await _startLocalPreflightPrint(mode, headPlan, stage); return; }
      await _startServerPreflightPrint(mode, headPlan, stage);
    }

    // The worker "rewrite" payload for a mode/plan - shared by "print" and
    // "download" so the file you download is byte-identical to the one that
    // would have been printed.
    function _rewritePayload(mode, headPlan) {
      const payload = {jobId: preflightJobId, file: preflightFile, mode};
      if (mode === "slicer") {
        // Same (possibly user-edited) remap the server path sends.
        const remap = {};
        for (const m of _slicerEffectiveMapping()) {
          if (!m.slot) continue;
          const synth = m.slot.ace * 4 + m.slot.slot;
          if (synth !== m.t) remap[String(m.t)] = synth;
        }
        payload.remapOverride = remap;
      } else if (mode === "head") {
        const hp = headPlan || "loadout";
        payload.headPlan = hp;
        if (hp === "loadout") {
          const asn = {};
          const eff = _headEffectiveAssignment();
          for (const k of Object.keys(eff)) { if (eff[k]) asn[String(k)] = eff[k]; }
          payload.headAssignment = asn;
        }
      }
      return payload;
    }

    // Browser path: rewrite in the worker, upload straight to Moonraker.
    // 32 lowercase hex chars, matching the backend's token/queue-id shape.
    // Not crypto.randomUUID(): that requires a secure context, and this app
    // is often reached over plain http on a LAN.
    function _hexId32() {
      let s = "";
      for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
      return s;
    }
    // fetch() has no upload-progress event (only download), so a large
    // upload has nothing to show but a frozen percent for however long it
    // takes - which is exactly what looked like a hang for a 200+ MB print.
    // XMLHttpRequest's upload.onprogress is the one thing that gives real
    // bytes-sent progress for a browser-originated upload, cross-browser
    // (including Safari, unlike a streaming fetch request body).
    function xhrUpload(url, formData, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", url);
        xhr.upload.onprogress = (ev) => {
          if (onProgress) onProgress(ev.loaded, ev.lengthComputable ? ev.total : 0);
        };
        xhr.onload = () => {
          let body = {};
          try { body = JSON.parse(xhr.responseText); } catch (_) {}
          resolve({ok: xhr.status >= 200 && xhr.status < 300,
                   status: xhr.status, statusText: xhr.statusText, body});
        };
        xhr.onerror = () => reject(new Error("network error"));
        xhr.onabort = () => reject(new Error("upload aborted"));
        xhr.send(formData);
      });
    }
    function _formatEta(seconds) {
      if (!isFinite(seconds) || seconds < 0) return "";
      if (seconds < 1) return "<1s";
      if (seconds < 60) return `${Math.ceil(seconds)}s`;
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return `${m}m ${s.toString().padStart(2, "0")}s`;
    }
    // Bytes-sent -> {percent within [base,base+span], eta label} tracker for
    // an upload progress bar. Smooths the instantaneous rate with an EMA so
    // the ETA doesn't jump around on every progress tick (those can arrive
    // in uneven bursts depending on the network stack).
    function _uploadEtaTracker(base, span) {
      let lastLoaded = 0, lastT = Date.now(), speed = 0;
      return (loaded, total) => {
        const now = Date.now();
        const dt = (now - lastT) / 1000;
        if (dt > 0.15) {
          const inst = Math.max(0, (loaded - lastLoaded) / dt);
          speed = speed ? (speed * 0.7 + inst * 0.3) : inst;
          lastLoaded = loaded; lastT = now;
        }
        const frac = total > 0 ? Math.min(1, loaded / total) : 0;
        const remaining = total > 0 && speed > 0 ? (total - loaded) / speed : null;
        return {
          percent: base + frac * span,
          eta: remaining != null ? _formatEta(remaining) : "",
        };
      };
    }
    async function _startLocalPreflightPrint(mode, headPlan, stage) {
      const rep = preflight.report;
      if (!rep || !preflightFile || !preflightJobId) return;
      preflight.sending = (mode === "head") ? (headPlan || "loadout") : mode;
      preflight.error   = "";
      preflight.progress = {percent: 0, stage: "queued", running: true};
      const startedAt = Date.now();
      const MIN_VISIBLE_MS = 1500;
      try {
        const payload = _rewritePayload(mode, headPlan);
        // The SET_PRINT_PREFERENCES prepend runs worker-side now (via the
        // same preflight_core.prepend_print_prefs the backend uses),
        // because the rewritten output is streamed back in chunks and is
        // never assembled into one JS string here to run a .replace() on.
        payload.bedMesh = !!preflight.bedMesh;
        payload.camera  = !!preflight.camera;
        const live = await loadLiveSlotsForPreflight();
        payload.liveSlots = live.live_slots;
        payload.headCtx   = live.head_ctx;
        // Same reasoning as the analyze payload above: re-read ace.cfg now
        // rather than trust whatever the worker last cached.
        payload.costParams = live.cost_params;
        const j = await runPreflightWorker("rewrite", payload, msg => {
          preflight.progress = {
            percent: Number(msg.percent || 0),
            stage:   String(msg.stage || ""), running: true};
        });
        preflight.progress = {percent: 90, stage: "upload", running: true, eta: ""};
        const displayName = rep.filename || preflightFile.name;
        const queueId = stage ? _hexId32() : "";
        const fd = new FormData();
        fd.append("root", "gcodes");
        fd.append("print", stage ? "false" : "true");
        if (stage) fd.append("path", "multiace-queue");
        fd.append("file",
          new Blob(j.chunks || [], {type: "application/octet-stream"}),
          stage ? `${queueId}-${displayName}` : displayName);
        const trackUpload = _uploadEtaTracker(90, 9);
        const r = await xhrUpload("/server/files/upload", fd, (loaded, total) => {
          const {percent, eta} = trackUpload(loaded, total);
          preflight.progress = {percent, stage: "upload", running: true, eta};
        });
        const body = r.body || {};
        if (!r.ok) {
          const detail = body.error || body.detail || body.message
            || `${r.status} ${r.statusText}`;
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        if (stage) {
          const rr = await fetch(`${API}/queue/register`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              id: queueId, filename: displayName,
              relpath: `multiace-queue/${queueId}-${displayName}`,
              mode, head_plan: (mode === "head") ? (headPlan || "loadout") : null,
              resolved_slots: j.resolvedSlots || [],
              live_slots: payload.liveSlots || [],
            }),
          });
          const rj = await rr.json().catch(() => ({}));
          if (!rr.ok) throw new Error(rj.detail || `${rr.status}`);
        }
        preflight.progress = {percent: 100, stage: "done", running: true, eta: ""};
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
        if (wait > 0) await new Promise(res => setTimeout(res, wait));
        setMacroLog(stage ? t("ui.queue.staged", {name: displayName})
                          : t("ui.upload.started", {name: displayName}));
        closePreflight();
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        preflight.sending = "";
        if (preflight.progress) preflight.progress.running = false;
      }
    }

    async function _startServerPreflightPrint(mode, headPlan, stage) {
      const rep = preflight.report;
      if (!rep || !rep.token) return;
      // For head mode the button identity is the head plan (loadout/optimize/
      // layer); for multi it is the mode.
      preflight.sending = (mode === "head") ? (headPlan || "loadout") : mode;
      preflight.error   = "";
      preflight.progress = {percent: 0, stage: "queued", running: true};
      const startedAt = Date.now();
      const MIN_VISIBLE_MS = 1500;
      const FIRST_POLL_MS  = 250;
      const POLL_MS        = 500;
      try {
        const body = {token: rep.token, mode, stage: !!stage,
                      bed_mesh: !!preflight.bedMesh, camera: !!preflight.camera};
        if (mode === "slicer") {
          // Send the (possibly user-edited) slot assignment verbatim so the
          // print matches the preview exactly. Only entries differing from
          // slot==head go into the remap.
          const remap = {};
          for (const m of _slicerEffectiveMapping()) {
            if (!m.slot) continue;
            const synth = m.slot.ace * 4 + m.slot.slot;
            if (synth !== m.t) remap[String(m.t)] = synth;
          }
          body.remap = remap;
        } else if (mode === "head") {
          const hp = headPlan || "loadout";
          body.head_plan = hp;
          if (hp === "loadout") {
            // Send the (possibly user-edited) colour->target assignment verbatim
            // so the print matches the preview exactly. Keys are slicer-T strings.
            const asn = {};
            const eff = _headEffectiveAssignment();
            for (const k of Object.keys(eff)) {
              if (eff[k]) asn[String(k)] = eff[k];
            }
            body.head_assignment = asn;
          }
          // optimize / layer: the server recomputes the proposed loadout, so we
          // send no assignment (the user has arranged spools to match it).
        }
        const r = await fetch(`${API}/preflight/print`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          throw new Error(j.detail || `${r.status} ${r.statusText}`);
        }
        const jobId = j.job_id;
        let last;
        let pollDelay = FIRST_POLL_MS;
        for (;;) {
          await new Promise(res => setTimeout(res, pollDelay));
          pollDelay = POLL_MS;
          let sr;
          try {
            sr = await fetch(`${API}/preflight/print/status?job_id=${encodeURIComponent(jobId)}`);
          } catch (_) {
            continue;
          }
          if (!sr.ok) {
            const sj = await sr.json().catch(() => ({}));
            throw new Error(sj.detail || `${sr.status} ${sr.statusText}`);
          }
          last = await sr.json();
          preflight.progress = {
            percent: Number(last.percent || 0),
            stage:   String(last.stage || ""),
            running: !last.done,
            eta:     last.eta_s != null ? _formatEta(Number(last.eta_s)) : "",
          };
          if (last.done) break;
        }
        if (last.error) throw new Error(last.error);
        preflight.progress = {percent: 100, stage: "done", running: true, eta: ""};
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
        if (wait > 0) {
          await new Promise(res => setTimeout(res, wait));
        }
        setMacroLog(last.queued ? t("ui.queue.staged", {name: rep.filename})
                                : t("ui.upload.started", {name: rep.filename}));
        closePreflight();
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        preflight.sending = "";
        if (preflight.progress) preflight.progress.running = false;
      }
    }

    // "Loadout übernehmen": the user has physically rearranged the spools to
    // match a proposed (optimize/layer) plan; write those identities onto the
    // ACE slots (slot-override) and, in head mode, onto the pinned feeder heads
    // (print_task_config). This only SETS filaments/colours - it does NOT start
    // a print.
    function _hex6(c) {
      let s = String(c || "").trim().toLowerCase();
      if (!s) return "";
      if (s[0] !== "#") s = "#" + s;
      if (s.length === 9) s = s.slice(0, 7); // #rrggbbaa -> #rrggbb
      return s;
    }
    function _loadoutOps(mode, headPlan) {
      // -> {overrides:[{ace,slot,material,color}], feeders:[{head,material,color}]}
      const overrides = [], feeders = [];
      const rep = preflight.report;
      if (!rep) return {overrides, feeders};
      if (mode === "head") {
        const plan = rep.plans[headPlan];
        if (!plan || !plan.mapping) return {overrides, feeders};
        for (const m of plan.mapping) {
          if (!m || m.kind === "none") continue;
          const color = _hex6(headSlicerHex(m.t));
          const mat = headSlicerMat(m.t);
          const material = (mat === "?") ? "" : mat;
          if (m.kind === "pin" && m.head !== null && m.head !== undefined) {
            feeders.push({head: m.head, material, color});
          } else if (m.kind === "ace" && m.ace !== null && m.ace !== undefined) {
            overrides.push({ace: m.ace, slot: m.slot, material, color});
          }
        }
      } else {
        const plan = rep.plans[mode];
        if (!plan || !plan.mapping) return {overrides, feeders};
        for (const m of plan.mapping) {
          if (!m || !m.slot) continue;
          overrides.push({
            ace: m.slot.ace, slot: m.slot.slot,
            material: m.slot.material || "", color: _hex6(m.slot.color),
          });
        }
      }
      return {overrides, feeders};
    }
    async function applyLoadout(mode, headPlan) {
      if (preflight.applying || preflight.sending) return;
      const ops = _loadoutOps(mode, headPlan);
      const total = ops.overrides.length + ops.feeders.length;
      if (!total) return;
      confirm({
        title:   t("ui.preflight.apply_loadout"),
        message: t("ui.preflight.apply_loadout_confirm", {count: total}),
        okLabel: t("ui.preflight.apply_loadout"),
        onOk: async () => {
          preflight.applying = (mode === "head") ? (headPlan || "loadout") : mode;
          try {
            for (const o of ops.overrides) {
              await fetch(`${API}/slot-override`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                  ace: o.ace, slot: o.slot, material: o.material,
                  brand: "", subtype: "", color: o.color,
                }),
              });
            }
            for (const f of ops.feeders) {
              const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
              const hex = (f.color || "#ffffff").replace("#", "");
              enqueue("SET_PRINT_FILAMENT_CONFIG", {
                CONFIG_EXTRUDER:     f.head,
                FILAMENT_TYPE:       dq(f.material || "PLA"),
                FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
                VENDOR:              dq("Generic"),
                FILAMENT_SUBTYPE:    dq(""),
              });
            }
            if (ops.overrides.length) {
              enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
            }
            setMacroLog(t("ui.preflight.apply_loadout_done", {count: total}));
            reloadState();
          } catch (e) {
            setMacroLog(`${t("ui.common.error")}: ${e}`);
          } finally {
            preflight.applying = "";
          }
        },
      });
    }
    let resizeObserver = null;

    // =================================================================
    // Shell: left navigation rail
    //
    // Icon AND label, always rendered. Not a preference: the primary
    // device is a touchscreen at the printer, and a hover-to-reveal
    // tooltip does not exist there. The collapse control exists for the
    // printer's own 1024x600 panel, where 168px is worth reclaiming.
    // =================================================================
    const RAIL_ICONS = {dashboard: "▤", monitor: "▣", history: "◷",
                        queue: "☰", spools: "◍", config: "⚙", plugin: "◫"};
    const railCollapsed = ref(localStorage.getItem("multiace.rail") === "1");
    function toggleRail() { railCollapsed.value = !railCollapsed.value; }
    // The class goes on <html>, not on .shell, because --rail-w has to
    // reach the position:fixed chrome that lives OUTSIDE the shell
    // (.cmd-queue-bar, .notif-strip).
    watch(railCollapsed, (v) => {
      localStorage.setItem("multiace.rail", v ? "1" : "0");
      document.documentElement.classList.toggle("rail-tight", v);
      // The rail's width changes the width of every card to its right.
      scheduleWiringRecompute();
    }, {immediate: true});

    const railItems = computed(() => {
      const items = [];
      if (state.mode !== "normal") {
        items.push({key: "dashboard", icon: RAIL_ICONS.dashboard,
                    label: t("ui.tabs.dashboard")});
      }
      items.push({key: "monitor", icon: RAIL_ICONS.monitor,
                  label: t("ui.tabs.monitor")});
      items.push({key: "spools", icon: RAIL_ICONS.spools,
                  label: t("ui.tabs.spools")});
      items.push({key: "history", icon: RAIL_ICONS.history,
                  label: t("ui.tabs.history")});
      items.push({key: "queue", icon: RAIL_ICONS.queue,
                  label: t("ui.tabs.queue")});
      items.push({key: "config", icon: RAIL_ICONS.config,
                  label: t("ui.tabs.config")});
      for (const p of plugins.items) {
        items.push({key: "plugin:" + p.name, icon: RAIL_ICONS.plugin,
                    label: p.label});
      }
      return items;
    });
    // While a load or unload is running the active segment takes the
    // colour of the filament that is actually moving, so the rail reports
    // machine state as well as position. The state already exists; the
    // cost is one custom property.
    const railSegColor = computed(() => {
      const ops = toolheadOps.value;
      for (const th of (state.toolheads || [])) {
        if (!ops[th.idx]) continue;
        if (th.color) return th.color;
      }
      return "";
    });
    const railSegStyle = computed(() => {
      const i = railItems.value.findIndex(it => it.key === tab.value);
      const st = {"--seg-i": String(Math.max(0, i))};
      if (i < 0) st["--seg-op"] = "0";
      if (railSegColor.value) st["--seg-color"] = railSegColor.value;
      return st;
    });

    // =================================================================
    // Collapsible panels
    //
    // Markup plus two helpers rather than a component: this app runs off
    // vue.global.prod.js with one createApp and zero registered
    // components, and introducing a component system for a handful of
    // call sites would be out of character.
    //
    // The four rarely-touched config sections default CLOSED; everything
    // you look at while standing at the printer defaults open.
    // =================================================================
    const SEC_DEFAULT_CLOSED = {
      "cfg.firmware": true,
      "cfg.tipform": true, "cfg.update": true,
    };
    const secClosed = reactive((() => {
      let stored = {};
      try { stored = JSON.parse(localStorage.getItem("multiace.sections") || "{}"); }
      catch (_) { stored = {}; }
      return Object.assign({}, SEC_DEFAULT_CLOSED, stored || {});
    })());
    const secOpen = (k) => !secClosed[k];
    function toggleSec(k) {
      secClosed[k] = !secClosed[k];
      localStorage.setItem("multiace.sections", JSON.stringify(secClosed));
    }
    // Config hint boxes: same persisted-collapse pattern as the panels
    // above, just inverted default - a setting's "what does this do"
    // paragraph starts HIDDEN and opens on click of the (i) button next to
    // its label, rather than starting open and collapsing.
    const infoOpen = reactive((() => {
      let stored = {};
      try { stored = JSON.parse(localStorage.getItem("multiace.info") || "{}"); }
      catch (_) { stored = {}; }
      return stored || {};
    })());
    const infoShown = (k) => !!infoOpen[k];
    function toggleInfo(k) {
      infoOpen[k] = !infoOpen[k];
      localStorage.setItem("multiace.info", JSON.stringify(infoOpen));
    }
    // Collapsing the lanes panel tears the slot/toolhead elements out of
    // the DOM, so the overlay has to be recomputed on the way back in -
    // scheduleWiringRecompute already does nextTick + rAF, which is
    // exactly what a collapse -> expand needs.
    watch(() => secClosed.lanes, scheduleWiringRecompute);
    // The container element itself comes and goes with the panel, so the
    // ResizeObserver has to follow it rather than being attached once.
    watch(wiringContainerEl, (el) => {
      if (!resizeObserver) return;
      try { resizeObserver.disconnect(); } catch (_) {}
      if (el) resizeObserver.observe(el);
    });

    // Collapsed headers keep their data. A bare title bar is a loss on a
    // status board: you collapsed to reclaim space, not to stop knowing.
    const laneChips = computed(() => {
      const out = [];
      for (const ace of visibleAces.value) {
        for (const slot of (ace.slots || [])) {
          out.push({
            color: slot.color || "",
            failed: false,
            title: `ACE ${dispIdx(ace.idx)} / ${dispIdx(slot.idx)}` +
                   (slot.material ? ` · ${slot.material}` : ""),
          });
        }
      }
      for (const th of (state.toolheads || [])) {
        if (!th.load_failed) continue;
        out.push({color: th.color || "", failed: true,
                  title: `T${dispIdx(th.idx)} · ${t("ui.dashboard.load_failed")}`});
      }
      return out;
    });
    // Which loadout is on the machine right now. Only ever set by
    // applying one from here, so it says what it knows and no more.
    const appliedSnapshot = ref(localStorage.getItem("multiace.appliedSnapshot") || "");

    // =================================================================
    // Unified dashboard: webcam + console side pane
    //
    // The point is to stop context-switching between Fluidd's camera and
    // this panel during a swap - so the pane is always on screen, as the
    // shell's right-hand column, mirroring the rail on the left. It is
    // not something you open: a pane you have to open first is one you
    // look at after the fact.
    //
    // Which of the three is showing is still real state, and still what
    // gates the work: no camera stream is opened and no console
    // subscription is sent for a pane that is not the visible one, so
    // exactly one of the three is ever live.
    // =================================================================
    // Live print control (plan section 8)
    //
    // Everything you can do by tapping the printer's own screen mid-print.
    // The values shown are READ BACK from /api/state, never remembered
    // locally: a change made on the physical display has to show up here,
    // and a local copy is how the two front-ends start disagreeing about
    // the machine.
    // =================================================================
    const printCtl = reactive({
      speed_factor: 100, extrude_factor: 100, fan: 0, z_offset: 0,
      nozzle: {temp: null, target: null}, bed: {temp: null, target: null},
      progress: 0, message: "", state: "", filename: "",
    });
    const printCtlLimits = reactive({
      ranges: {speed: {min: 25, max: 300}, flow: {min: 75, max: 125},
               fan: {min: 0, max: 100}},
      nozzle: {min: 0, max: 300}, bed: {min: 0, max: 120},
      babystep: {step: 0.05, total: 0.5, used: 0},
    });
    // Local slider positions. Only these are local - they follow the
    // readback except while the user is actually dragging one.
    const printCtlDraft = reactive({speed: 100, flow: 100, fan: 0});
    const printCtlDragging = ref("");
    const printCtlBusy = ref("");
    const printCtlError = ref("");

    function applyPrintControlState(s) {
      const pc = s && s.print_control;
      if (!pc) return;
      Object.assign(printCtl, pc);
      // Do not fight the finger: leave the slider the user is holding
      // where they put it.
      if (printCtlDragging.value !== "speed") printCtlDraft.speed = pc.speed_factor;
      if (printCtlDragging.value !== "flow")  printCtlDraft.flow  = pc.extrude_factor;
      if (printCtlDragging.value !== "fan")   printCtlDraft.fan   = pc.fan;
    }

    async function loadPrintControlLimits() {
      try {
        const r = await fetch(`${API}/print-control/limits`);
        if (r.ok) Object.assign(printCtlLimits, await r.json());
      } catch (_) {}
    }

    async function sendPrintControl(verb, value) {
      printCtlBusy.value = verb;
      printCtlError.value = "";
      try {
        const r = await fetch(`${API}/print-control`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(value === undefined ? {verb} : {verb, value}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        // Snap the slider to what the server ACTUALLY applied - it clamps,
        // and a control showing a value the printer refused is a control
        // that lies about the machine.
        if (j.applied !== null && j.applied !== undefined) {
          if (verb === "speed") printCtlDraft.speed = j.applied;
          if (verb === "flow")  printCtlDraft.flow  = j.applied;
          if (verb === "fan")   printCtlDraft.fan   = j.applied;
        }
        if (j.ok === false && j.detail) printCtlError.value = j.detail;
        if (verb === "babystep" && j.total_mm !== undefined) {
          printCtlLimits.babystep.used = j.total_mm;
        }
        await reloadState();
        return j;
      } catch (e) {
        printCtlError.value = e.message || String(e);
      } finally {
        printCtlBusy.value = "";
      }
    }

    // One command per ~250 ms of dragging, and ALWAYS a final command on
    // release: an undelivered last event is how a slider lies about the
    // machine's state.
    let _ctlDebounce = null;
    function printCtlSlide(verb, value) {
      printCtlDragging.value = verb;
      if (_ctlDebounce) clearTimeout(_ctlDebounce);
      _ctlDebounce = setTimeout(() => sendPrintControl(verb, Number(value)), 250);
    }
    function printCtlRelease(verb, value) {
      if (_ctlDebounce) { clearTimeout(_ctlDebounce); _ctlDebounce = null; }
      printCtlDragging.value = "";
      sendPrintControl(verb, Number(value));
    }
    function printCtlReset(verb) {
      printCtlRelease(verb, verb === "fan" ? 0 : 100);
    }
    function printCtlCancel() {
      confirm({
        title: t("ui.print_control.cancel_title"),
        message: t("ui.print_control.cancel_msg"),
        okLabel: t("ui.print_control.cancel_ok"),
        onOk: () => sendPrintControl("cancel"),
      });
    }
    // Pause deliberately gets NO confirm: pausing is recoverable, and a
    // dialog on the control you reach for in a hurry is its own hazard.

    const printControlShown = computed(() =>
      !panelMode && tab.value === "monitor");
    watch(printControlShown, on => { if (on) loadPrintControlLimits(); });

    // =================================================================
    // Print history (plan section 4)
    //
    // Moonraker owns duration and result; multiACE owns the plan, the
    // assignment and the swaps. This tab shows the join, not a copy.
    // =================================================================
    const history = reactive({
      loaded: false, busy: false, error: "", jobs: [], printerUi: "",
      detail: null, reprinting: "",
    });

    async function loadHistory() {
      history.busy = true;
      history.error = "";
      try {
        const r = await fetch(`${API}/history?limit=50`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        history.jobs = j.jobs || [];
        history.printerUi = j.printer_ui || "";
        history.loaded = true;
      } catch (e) {
        history.error = e.message || String(e);
      } finally {
        history.busy = false;
      }
    }
    function openHistoryDetail(row) {
      history.detail = (history.detail && history.detail.id === row.id)
        ? null : row;
    }
    function historyColors(row) {
      const asn = (row && row.multiace && row.multiace.assignment) || {};
      return Object.keys(asn).map(h => ({
        head: h, ace: asn[h].ace_index, slot: asn[h].slot,
        color: asn[h].color ? ("#" + String(asn[h].color).replace(/^#/, ""))
                            : "#666",
      }));
    }
    // Estimate vs actual, for the one line that tells you whether the
    // number on the preflight screen was worth anything.
    function historyAccuracy(row) {
      const est = row && row.multiace && row.multiace.estimate;
      const actual = row && row.duration;
      if (!est || !est.total_s || !actual) return null;
      const pct = Math.round(((actual - est.total_s) / est.total_s) * 1000) / 10;
      return {predicted: est.total_s, actual, pct};
    }
    watch(() => tab.value, v => { if (v === "history" && !history.loaded) loadHistory(); });
    // Reprint: start the file exactly as it already sits on the printer -
    // no preflight, no rewrite. The swaps baked into it from its first run
    // still apply; a stale loadout since then is the user's call, not ours.
    function reprintJob(row) {
      if (!row || !row.filename || history.reprinting) return;
      confirm({
        title: t("ui.history.reprint_title"),
        message: t("ui.history.reprint_msg", {name: row.filename}),
        okLabel: t("ui.history.reprint_ok"),
        onOk: () => _doReprint(row),
      });
    }
    async function _doReprint(row) {
      history.reprinting = row.id || row.job_id || row.filename;
      history.error = "";
      try {
        const r = await fetch(`${API}/history/reprint`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({filename: row.filename}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        setMacroLog(t("ui.history.reprint_started", {name: row.filename}));
      } catch (e) {
        history.error = e.message || String(e);
      } finally {
        history.reprinting = "";
      }
    }

    // =================================================================
    // Print queue: staged, already-rewritten gcode waiting to be launched
    // (staged from the preflight dialog's "stage for later" action).
    // =================================================================
    const queue = reactive({
      loaded: false, busy: false, error: "", jobs: [], launching: "", deleting: "",
    });
    async function loadQueue() {
      queue.busy = true;
      queue.error = "";
      try {
        const r = await fetch(`${API}/queue`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        queue.jobs = j.jobs || [];
        queue.loaded = true;
      } catch (e) {
        queue.error = e.message || String(e);
      } finally {
        queue.busy = false;
      }
    }
    watch(() => tab.value, v => { if (v === "queue" && !queue.loaded) loadQueue(); });

    function queueDriftTooltip(row) {
      const d = row && row.drift;
      if (!d || !d.changed || !d.slots) return "";
      return d.slots.map(s => {
        const fmt = (v) => (v && (v.material || v.color))
          ? `${v.material || "?"} ${v.color || ""}`.trim() : t("ui.queue.slot_empty");
        return t("ui.queue.drift_tooltip_line", {
          ace: dispIdx(s.ace), slot: dispIdx(s.slot),
          was: fmt(s.was), now: fmt(s.now)});
      }).join("\n");
    }

    async function _doLaunchQueued(row) {
      queue.launching = row.id;
      queue.error = "";
      try {
        const r = await fetch(`${API}/queue/${row.id}/launch`, {method: "POST"});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        setMacroLog(t("ui.queue.launch_ok", {name: row.filename}));
        await loadQueue();
      } catch (e) {
        queue.error = e.message || String(e);
      } finally {
        queue.launching = "";
      }
    }
    function launchQueued(row) {
      if (!row || queue.launching || row.missing_on_printer) return;
      if (row.drift && row.drift.changed) {
        confirm({
          title:   t("ui.queue.launch_drifted_title"),
          message: t("ui.queue.launch_drifted_msg", {name: row.filename}),
          okLabel: t("ui.queue.launch"),
          onOk:    () => _doLaunchQueued(row),
        });
      } else {
        _doLaunchQueued(row);
      }
    }

    async function _doDeleteQueued(row) {
      queue.deleting = row.id;
      queue.error = "";
      try {
        const r = await fetch(`${API}/queue/${row.id}`, {method: "DELETE"});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        await loadQueue();
      } catch (e) {
        queue.error = e.message || String(e);
      } finally {
        queue.deleting = "";
      }
    }
    function deleteQueued(row) {
      if (!row || queue.deleting) return;
      confirm({
        title:   t("ui.queue.delete_title"),
        message: t("ui.queue.delete_msg", {name: row.filename}),
        okLabel: t("ui.queue.delete"),
        onOk:    () => _doDeleteQueued(row),
      });
    }

    const webcam = reactive({
      loaded: false, available: false, reason: "", name: "",
      service: "", webrtc_url: "", connFailed: false,
      // Cache-buster: an MJPEG <img> that was torn down keeps its old
      // connection cached, so re-opening the pane shows a frozen frame
      // until the src actually changes.
      nonce: 0,
    });
    const webcamVideo = ref(null);
    const webcamShown = computed(() =>
      !panelMode && tab.value === "monitor");
    const webcamSrc = computed(() =>
      webcam.available ? `${API}/webcam/stream?n=${webcam.nonce}` : "");
    let webcamPc = null;
    function stopWebrtc() {
      if (webcamPc) { webcamPc.close(); webcamPc = null; }
    }
    // camera-streamer's handshake is reversed from a typical WHIP/WHEP
    // flow: the SERVER makes the offer (it knows the video track) and
    // the browser answers, over two plain POSTs of JSON-wrapped SDP.
    async function startWebrtc() {
      stopWebrtc();
      webcam.connFailed = false;
      await nextTick();
      const video = webcamVideo.value;
      if (!video) return;
      const signalUrl = `${API}/webcam/webrtc-signal`;
      const pc = new RTCPeerConnection();
      webcamPc = pc;
      pc.addTransceiver("video", {direction: "recvonly"});
      pc.addEventListener("track", (evt) => {
        if (pc === webcamPc && evt.track.kind === "video") video.srcObject = evt.streams[0];
      });
      // "track" fires as soon as the recvonly transceiver is negotiated,
      // before any media actually arrives - it is not proof the camera is
      // reachable. connectionState is: some LAN cameras' ICE stacks never
      // resolve Chrome's mDNS-obfuscated host candidates, so the peer
      // connection can sit in "new"/"checking" forever with no "failed"
      // event to react to. A flat timeout is what actually catches that.
      const connectTimeout = setTimeout(() => {
        if (pc === webcamPc && pc.connectionState !== "connected") {
          webcam.reason = "connection timed out";
          webcam.connFailed = true;
        }
      }, 10000);
      pc.addEventListener("connectionstatechange", () => {
        if (pc !== webcamPc) return;
        if (pc.connectionState === "connected") {
          clearTimeout(connectTimeout);
          webcam.connFailed = false;
        } else if (["failed", "disconnected"].includes(pc.connectionState)) {
          clearTimeout(connectTimeout);
          webcam.connFailed = true;
          if (webcamShown.value) {
            setTimeout(() => { if (pc === webcamPc) startWebrtc(); }, 1000);
          }
        }
      });
      try {
        const offerRes = await fetch(signalUrl, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({type: "request"}),
        });
        const offer = await offerRes.json();
        await pc.setRemoteDescription(offer);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        await new Promise((resolve) => {
          if (pc.iceGatheringState === "complete") { resolve(); return; }
          const check = () => {
            if (pc.iceGatheringState === "complete") {
              pc.removeEventListener("icegatheringstatechange", check);
              resolve();
            }
          };
          pc.addEventListener("icegatheringstatechange", check);
        });
        if (pc !== webcamPc) return;
        await fetch(signalUrl, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({type: pc.localDescription.type, id: offer.id,
                                 sdp: pc.localDescription.sdp}),
        });
      } catch (e) {
        if (pc === webcamPc) {
          webcam.reason = e.message || String(e);
          webcam.connFailed = true;
        }
      }
    }
    async function loadWebcam() {
      try {
        const r = await fetch(`${API}/webcam/info`);
        const j = r.ok ? await r.json() : {available: false, reason: `HTTP ${r.status}`};
        webcam.available = !!j.available;
        webcam.reason = j.reason || "";
        webcam.name = j.name || "";
        webcam.service = j.service || "";
        webcam.webrtc_url = j.webrtc_url || "";
      } catch (e) {
        webcam.available = false;
        webcam.reason = String(e);
        webcam.service = "";
        webcam.webrtc_url = "";
      } finally {
        webcam.loaded = true;
        webcam.nonce++;
        if (webcamShown.value && webcam.available && webcam.service === "webrtc") startWebrtc();
        else stopWebrtc();
      }
    }
    watch(webcamShown, (shown) => {
      if (shown) {
        if (!webcam.loaded) loadWebcam();
        else if (webcam.service === "webrtc") startWebrtc();
        else webcam.nonce++;
      } else {
        stopWebrtc();
      }
    });
    onUnmounted(stopWebrtc);

    const CONSOLE_MAX = 400;
    const consoleLines = ref([]);
    const consoleFollow = ref(localStorage.getItem("multiace.console.follow") !== "0");
    const consoleInput = ref("");
    const consoleBusy = ref(false);
    const consoleEl = ref(null);
    const consoleShown = computed(() =>
      !panelMode && tab.value === "monitor");
    let lastConsoleId = 0;
    watch(consoleFollow,
          v => localStorage.setItem("multiace.console.follow", v ? "1" : "0"));
    function _appendConsole(lines) {
      if (!Array.isArray(lines) || !lines.length) return;
      for (const ln of lines) {
        if (ln.id != null && ln.id <= lastConsoleId) continue;
        if (ln.id != null) lastConsoleId = ln.id;
        consoleLines.value.push(ln);
      }
      if (consoleLines.value.length > CONSOLE_MAX) {
        consoleLines.value.splice(0, consoleLines.value.length - CONSOLE_MAX);
      }
      if (consoleFollow.value) {
        nextTick(() => {
          const el = consoleEl.value;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    }
    async function loadConsole() {
      try {
        const r = await fetch(`${API}/console-logs?lines=200&since_id=${lastConsoleId}`);
        if (!r.ok) return;
        _appendConsole((await r.json()).lines || []);
      } catch (_) {}
    }
    function _sendConsoleSubscription() {
      try {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({type: "subscribe", console: consoleShown.value}));
        }
      } catch (_) {}
    }
    watch(consoleShown, (shown) => {
      _sendConsoleSubscription();
      if (shown) loadConsole();
    });
    async function sendConsole() {
      const script = consoleInput.value.trim();
      if (!script || consoleBusy.value) return;
      consoleBusy.value = true;
      try {
        const r = await fetch(`${API}/console`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({script}),
        });
        if (!r.ok) {
          _appendConsole([{id: null, ts: Date.now() / 1000, kind: "error",
                           msg: `!! ${await r.text()}`}]);
        }
        consoleInput.value = "";
      } catch (e) {
        _appendConsole([{id: null, ts: Date.now() / 1000, kind: "error",
                         msg: `!! ${e}`}]);
      } finally { consoleBusy.value = false; }
    }
    function consoleTime(ts) {
      if (!ts) return "";
      try { return new Date(ts * 1000).toLocaleTimeString(); }
      catch (_) { return ""; }
    }

    // =================================================================
    // Auto-retry controls
    //
    // The retry runs inside Klipper's load command, so these go through
    // the backend's control file rather than G-code - a G-code line
    // would only be read after the load it is meant to steer has ended.
    // =================================================================
    const retryBusy = ref("");
    const retryPct = computed(() => {
      const r = retryState.value;
      if (!r || !r.max_attempts) return 0;
      return Math.min(100, Math.round((r.attempt / r.max_attempts) * 100));
    });
    async function retryControl(action) {
      if (retryBusy.value) return;
      retryBusy.value = action;
      try { await fetch(`${API}/retry/${action}`, {method: "POST"}); }
      catch (_) {}
      finally { setTimeout(() => { retryBusy.value = ""; }, 500); }
    }

    // ACE_RESCAN from the §7 banner. Users should not have to know a
    // macro exists to recover from "I switched the ACE on afterwards".
    async function aceRescan() {
      if (aceRescanBusy.value) return;
      aceRescanBusy.value = true;
      try {
        await fetch(`${API}/plugin-api/gcode`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({script: "ACE_RESCAN"}),
        });
        await reloadState();
      } catch (_) {}
      finally { aceRescanBusy.value = false; }
    }

    // =================================================================
    // Firmware compatibility
    // =================================================================
    const firmware = reactive({
      version: "", status: "unknown", known_issues: [], source: "",
      device: "", reason: "",
    });
    const firmwareWarn = computed(() =>
      firmware.status === "unsupported" || firmware.status === "untested");

    // =================================================================
    // Apply-changes flow
    //
    // Saving used to mean "file written, now go reboot, good luck". The
    // modal names what changed and asks for the WEAKEST restart that
    // actually applies it - most edits need a Klipper restart, a few
    // need nothing at all, and only unit-count/serial changes need a
    // full reboot.
    // =================================================================
    const applyModal = reactive({
      open: false,
      busy: false,
      // "preview" (about to save) | "saved" (written, restart pending)
      // | "restarting" (waiting for the printer to come back)
      phase: "preview",
      changes: [],
      restartRequired: "none",
      error: "",
      pendingContent: "",
      pendingSha1: "",
      reconnectSecs: 0,
    });
    const RESTART_LABELS = {
      none: "ui.config.restart_none",
      klipper_restart: "ui.config.restart_klipper_needed",
      printer_reboot: "ui.config.restart_reboot_needed",
    };
    function restartLabel(kind) {
      return t(RESTART_LABELS[kind] || RESTART_LABELS.none);
    }
    function closeApplyModal() {
      applyModal.open = false;
      applyModal.error = "";
    }
    async function _putConfig(content, sha1, behavior) {
      const body = JSON.stringify({content, base_sha1: sha1,
                                   restart_behavior: behavior});
      let r = await fetch(`${API}/config`, {
        method: "PUT", headers: {"Content-Type": "application/json"}, body,
      });
      if (r.status === 409) {
        // Same lost-update guard as before: rebase the form values on the
        // current on-disk text and retry once.
        let fresh = null;
        try { fresh = JSON.parse((await r.json()).detail); } catch (_) { fresh = null; }
        if (!fresh || typeof fresh.content !== "string") {
          throw new Error(`HTTP 409 ${t("ui.log.config_conflict_failed")}`);
        }
        const rebased = formToCfgContent(fresh.content);
        r = await fetch(`${API}/config`, {
          method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({content: rebased, base_sha1: fresh.sha1 || "",
                                restart_behavior: behavior}),
        });
        configLog.value = t("ui.log.config_conflict_merged");
        if (r.ok) return {resp: r, content: rebased};
      }
      return {resp: r, content};
    }
    // Save, then show what changed and what it needs. The save itself
    // never restarts anything ("none"): the user decides in the modal.
    async function saveConfigForm() {
      configLog.value = t("ui.common.saving");
      applyModal.error = "";
      try {
        const newContent = formToCfgContent(config.content);
        const {resp, content} = await _putConfig(newContent, config.sha1, "none");
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${await resp.text()}`);
        const j = await resp.json();
        config.content = content;
        config.sha1 = j.sha1 || "";
        applyModal.changes = j.changes || [];
        applyModal.restartRequired = j.restart_required || "none";
        applyModal.phase = "saved";
        applyModal.open = true;
        rebootNeeded.value = (applyModal.restartRequired !== "none");
        configLog.value = `✓ ${j.path}\nBackup: ${j.backup}`;
        // Nothing to restart -> say so and get out of the way.
        if (applyModal.restartRequired === "none") {
          configLog.value += `\n${t("ui.config.restart_none")}`;
          setTimeout(() => { if (applyModal.phase === "saved") closeApplyModal(); }, 2500);
        }
      } catch (e) {
        applyModal.error = String(e);
        configLog.value = `${t("ui.common.error")}: ${e}`;
      }
    }
    // "Restart now" from the modal. Klipper restart and full reboot both
    // drop the websocket; the UI switches to a reconnect countdown and
    // recovers on its own instead of leaving a dead page behind.
    async function applyRestart(behavior) {
      if (applyModal.busy) return;
      applyModal.busy = true;
      applyModal.error = "";
      try {
        const r = await fetch(`${API}/restart`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({behavior}),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        applyModal.phase = "restarting";
        applyModal.reconnectSecs = 0;
        rebootNeeded.value = false;
        _awaitPrinterBack();
      } catch (e) {
        applyModal.error = String(e);
      } finally { applyModal.busy = false; }
    }
    // Poll /api/health until the printer answers again (5 min cap - past
    // that something needs a human, and a spinner forever is a lie).
    let _reconnectTimer = null;
    function _awaitPrinterBack() {
      clearInterval(_reconnectTimer);
      const started = Date.now();
      _reconnectTimer = setInterval(async () => {
        applyModal.reconnectSecs = Math.round((Date.now() - started) / 1000);
        if (applyModal.reconnectSecs > 300) {
          clearInterval(_reconnectTimer);
          applyModal.error = t("ui.config.restart_timeout");
          return;
        }
        try {
          const r = await fetch(`${API}/state`, {cache: "no-store"});
          if (!r.ok) return;
          const s = await r.json();
          if (s.klippy === "disconnected" || s.error) return;
          clearInterval(_reconnectTimer);
          applyState(s);
          await loadConfig();
          closeApplyModal();
        } catch (_) {}
      }, 2000);
    }

    // Direct restart buttons (dashboard toolbar, ACE-not-found banner):
    // same /api/restart the config-apply modal uses, just without the
    // modal - a plain confirm, then _awaitPrinterBack does the same
    // reconnect-and-refresh it always does. Guarded on isPrinting the
    // same way unload/load already are - both restarts drop the MCU
    // connection and would abort a running print.
    async function _fireRestart(behavior, sentKey) {
      const r = await fetch(`${API}/restart`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({behavior}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
      setMacroLog(t(sentKey));
      _awaitPrinterBack();
    }
    function restartFirmware() {
      if (isPrinting.value) return;
      confirm({
        title: t("ui.restart.firmware_title"),
        message: t("ui.restart.firmware_msg"),
        okLabel: t("ui.restart.firmware_btn"),
        onOk: async () => {
          try { await _fireRestart("klipper_restart", "ui.restart.firmware_sent"); }
          catch (e) { setMacroLog(`${t("ui.common.error")}: ${e.message || e}`); }
        },
      });
    }
    function restartPrinter() {
      if (isPrinting.value) return;
      confirm({
        title: t("ui.restart.printer_title"),
        message: t("ui.restart.printer_msg"),
        okLabel: t("ui.restart.printer_btn"),
        onOk: async () => {
          try { await _fireRestart("printer_reboot", "ui.restart.printer_sent"); }
          catch (e) { setMacroLog(`${t("ui.common.error")}: ${e.message || e}`); }
        },
      });
    }

    // =================================================================
    // Debug panel (?debug=1): connection facts and, in mock mode, event
    // injection - the only way to exercise the retry UI without a jam.
    // =================================================================
    const debugPanel = reactive({
      enabled: _q.get("debug") === "1",
      open: false,
      mock: false,
      lastMessage: 0,
      messages: 0,
      log: "",
    });
    // The highest-value dev affordance here: the preview parses a File the
    // user picked, and reaching it from a cold start otherwise means
    // finding a g-code file and walking the whole upload -> preflight flow
    // every single time. This fetches the 4-colour Snapmaker Orca fixture
    // and hands it to the SAME path a real pick takes, so what gets
    // exercised is the real code and not a shortcut around it.
    const sampleBusy = ref(false);
    async function loadSampleGcode() {
      if (sampleBusy.value) return;
      sampleBusy.value = true;
      debugPanel.log = "";
      try {
        const r = await fetch(`${API}/debug/sample-gcode`);
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        const blob = await r.blob();
        const f = new File([blob], "sample_4color.gcode",
                           {type: "text/plain"});
        debugPanel.open = false;
        onUploadGcode([f]);
      } catch (e) {
        debugPanel.log = String(e && e.message ? e.message : e);
      } finally {
        sampleBusy.value = false;
      }
    }
    async function simulateEvent(event, extra) {
      try {
        const r = await fetch(`${API}/debug/simulate`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({event, ...(extra || {})}),
        });
        debugPanel.log = r.ok ? `✓ ${event}` : `${r.status}: ${await r.text()}`;
      } catch (e) { debugPanel.log = String(e); }
    }
    function debugSince() {
      if (!debugPanel.lastMessage) return "—";
      return `${Math.round((Date.now() - debugPanel.lastMessage) / 100) / 10}s`;
    }

    onMounted(async () => {
      // Page loaded straight into the spools tab (stored tab) - the watcher
      // never fires for that, so trigger the idle push here too.
      if (tab.value === "spools") spoolmanIdlePush();
      acefwLoadVersions();
      await loadLanguageList();
      // No explicit browser choice yet -> follow the printer's persisted
      // language (ace__language), so a fresh browser opens in the same
      // language as the printer instead of defaulting to English.
      if (!localStorage.getItem("multiace.lang")) {
        try {
          const r = await fetch(`${API}/state`);
          if (r.ok) {
            const s = await r.json();
            if (s && s.language) language.value = s.language;
          }
        } catch (_) {}
      }
      await loadCatalog(language.value);
      try {
        const r = await fetch(`${API}/version`);
        if (r.ok) {
          const j = await r.json();
          version.value = `v${j.web}`;
          const p = j.printer || {};
          printerName.value = p.device_name || "";
          printerFw.value   = p.firmware_version || "";
          const fw = j.firmware || {};
          firmware.version = fw.firmware_version || printerFw.value || "";
          firmware.status = fw.status || "unknown";
          firmware.known_issues = fw.known_issues || [];
          firmware.source = fw.source || "";
          firmware.device = fw.device || "";
          firmware.reason = fw.reason || "";
          debugPanel.mock = !!j.mock;
        }
      } catch (_) {}
      try { const r = await fetch(`${API}/screen-available`); if (r.ok) screenAvailable.value = (await r.json()).available; } catch (_) {}
      await reloadState();
      await reloadSnapshots();
      await loadConfig();
      await loadTipform();
      await loadMaterials();
      await loadNotifications();
      await refreshDebugState();
      await refreshPlugins();
      if (state.mode === "normal" && tab.value === "dashboard") tab.value = "config";
      // Sidebar panes are lazy, but one that was left open in a previous
      // session should come back populated, not empty.
      if (webcamShown.value) loadWebcam();
      if (consoleShown.value) loadConsole();
      wsConnect();
      if (window.ResizeObserver && wiringContainerEl.value) {
        resizeObserver = new ResizeObserver(() => recomputeWiring());
        resizeObserver.observe(wiringContainerEl.value);
      } else {
        window.addEventListener("resize", recomputeWiring);
      }
      scheduleWiringRecompute();
      document.addEventListener("pointerdown", _smDocPointerDown, true);
      document.addEventListener("keydown", _smDocKeydown);
      // Never in the panel. The guard is there to stop you navigating the
      // MAIN UI away mid-operation; embedded as a Fluidd camera card we have
      // no say over the parent's navigation anyway, and there is nothing to
      // lose in a tile - the commands run on the printer, not in the browser.
      // Worse, beforeunload fires every time OUR document is torn down, and
      // Fluidd drops the card's iframe src whenever it pauses its streams -
      // which a browser tab switch does. With anything left in the queue that
      // was a "leave site?" prompt on every single tab switch (Dirk, HW).
      if (!panelMode) window.addEventListener("beforeunload", _onBeforeUnload);
    });
    function _onBeforeUnload(ev) {
      const pending = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (!pending) return;
      ev.preventDefault();
      ev.returnValue = '';
      return '';
    }
    onUnmounted(() => {
      clearTimeout(wsReconnectTimer);
      clearInterval(screenTimer);
      clearInterval(_reconnectTimer);
      try { ws?.close(); } catch (_) {}
      try { resizeObserver?.disconnect(); } catch (_) {}
      window.removeEventListener("resize", recomputeWiring);
      window.removeEventListener("beforeunload", _onBeforeUnload);
    });
    return {
      subText,
      sourceLabel,
      tab, version, printerName, printerFw, connClass, connText, screenAvailable,
      state, loadError, run, macroLog,
      panelMode, panelAce, panelAceIdx, panelSlotHead, panelPages, panelPage, panelPageId, panelFeederHeads, setPanelPage,
      panelSlotHeadLoaded, panelSlotActive, panelSlotLabel, panelSlotOp,
      panelMini, fullUiHref,
      slotTitle, switchAce, loadSlot, slotIsEmpty, loadFeederHead, loadComboFeederHead, slotLoadedInHead, loadAll, unloadHead, unloadAll, setHeadManual, setHeadFeeder, setHeadFeederCombo, headFeederComboOf, setHeadAce, headToggle, aceOptionsForHead, headAceOf, aceProtoTitle, visibleAces, openHeadPicker, isToolheadOccupied, needsReload, toolheadOps, bgEnabledFor, setBgHead, setPickupCleaning, setConfirmCommands, setRememberFilament, setAutoDry, autoDryValue, autoDryInput, autoDryCommit, autoDryPairInvalid, autoDryFieldError, autoDryEnable, autoDrySetMaster, autoDryMasters, spoolmanUrl, spoolmanBusy, spoolmanStatusText, saveSpoolmanUrl, setSpoolmanAuto, spoolmanSync,
      spoolmanConnected, spoolmanUrlSet, setSpoolMode, smQuery, smRows, smBusy, smOpen, smSearchDebounced, smAdopt,
      smPing, smPingInfo, spoolmanPing, spoolQuery, smPick, smPickTarget, smAdoptStaged,
      spoolBadgeCls, spoolBadgeLabel, headTileEmpty, setAirprintDetection, setQuadReplenish, setQuadFirst, setPurgeMatrix, FILAMENT_SWATCHES, knownColors, sameSwatch, pickerRfidSku, pickerHeadTag, headRfidBusy, headRfidNote, readHeadRfid,
      addFluiddCamera, fluiddCamBusy, fluiddCamMsg,
      spoolForm, spoolImportMode, spoolFileInput,
      spoolMaterials, spoolVendors, spoolSubtypes,
      spoolListShown, spoolFilterActive, spoolFormClear,
      spoolList, spoolForSlot, spoolForHead, spoolSlotLabel, spoolWeightLabel, spoolTitle,
      spoolAdd, spoolSave, spoolEdit, spoolEditCancel, spoolEditId, spoolAddBusy,
      spoolUnassign, spoolDelete, spoolDetails,
      spoolSlotOptions, spoolSlotKey, spoolMoveLocked, spoolAssignTo, spoolPickerOptions,
      smRowTaken, smRowWhere, spoolWeightDialog, spoolSort, spoolNewTitle,
      spoolCreating, spoolNewForm,
      acefw, acefwInput, acefwCandidates, acefwPickFile, acefwUpload,
      acefwCanTest, acefwReady, acefwTest, acefwFlash, acefwStatusText,
      acefwVersions,
      spoolCreateFromPicker,
      spoolExport, spoolImport, triggerSpoolImport,
      isPrinting,
      dryerCfg, dryStart, dryStop, dryPanelOpen, toggleDryPanel, aceDrying,
      feederRetractLengthEffective,
      snapshots, selectedSnapshot, snapshotPreview, saveSnapshot, loadSnapshot, deleteSnapshot,
      config, configLog, configLoadError, showRawConfig, configForm, rebootNeeded,
      aceHeadsRightSide,
      loadConfig, saveConfigForm, saveConfigRaw, setMode,
      tipform, loadTipform, tipformAddRow, tipformRemoveRow, saveTipform, tipformRestartPending, tipformNameOptions,
      TIPFORM_STEP_TYPES, tipformToggleBuilder, tipformAddStep, tipformRemoveStep, tipformStepsToTable, tipformStepPlaceholder, tipformInsertStock,
      preflight, closePreflight, startPreflightPrint, applyLoadout, stageLabel,
      fmtDuration, estimateDelta, wastePercent,
      gpreview, gpreviewCanvasEl, startGcodePreview, closeGcodePreview,
      gpreviewToggleCollapse, gpreviewSetAutoStart, gpreviewAutoEnabled, whatifRerun,
      gpreviewTogglePlay, gpreviewSetSpeed, gpreviewLegend, gpreviewFeatureLegend,
      gpreviewSetLayerLo, gpreviewSetLayerHi, gpreviewPreset,
      gpreviewIsolate, gpreviewToggleMax, gpreviewLod, gpreviewFeasible,
      swimLanes, swapColor, swapTitle,
      virtualLoadout, virtualSeed, virtualAceCount, virtualExport,
      tierLabel, tierWarn, rgbDec, sortedMapping, slicerColorsInPrintOrder,
      forcaMixed, forcaNozzleList, forcaGroups, forcaFeasible, forcaTargetHint,
      forcaSwapsDisplay, forcaBgLabel, forcaFlushG,
      forcaGroupAces,
      headOfSlot, forcaAllowedHeads, forcaHeadsForMaterial, unsetSlotsForT,
      slotKey, textOn, slicerSlotOptions, slicerEffectiveSlot, onSlicerSlotChange,
      slicerSwapsDisplay,
      headTargets, headTargetOptions, headEffectiveTargetId, headTargetLabel,
      headTargetColor, headTargetLabelById, onHeadTargetChange, headSwapsDisplay,
      hmDropOpen, hmDropPos, hmDdToggle, hmDdClose, hmDdPick,
      headFeasible, headPlanFeasible, headPlanSwaps, headPlanBg, headPlanFlushG, headPlanBgLabel, headSlicerHex,
      headSlicerMat, headProposalLabel,
      cmapDetails, colorMapRows, cmapBands, cmapSummary, cmapEdited, cmapResetAuto, cmapPick,
      strategy, strategyTabs, selectedPlan, loadoutMoves,
      canPrint, canApplyLoadout, printSelected, stageSelected, applySelected,
      updateState, updateCheck, updateApply,
      debugState, debugEnable, debugDisable, debugReboot,
      plugins, refreshPlugins, pluginIframeSrc,
      notifications, notifWarnOnly, notifTime, dismissNotification, dismissAllNotifications,
      confirmDialog, okConfirm, altConfirm, cancelConfirm, confirmInputError,
      screenCanvas, floatScreenCanvas, screenPopout, toggleScreenPopout,
      popoutStyle, popoutDragStart, popoutDragMove, popoutDragEnd,
      screenFps, screenEtag,
      screenDown, screenMove, screenUp,
      wiringContainerEl, setSlotEl, setThEl, wiringPaths, wiringViewBox,
      t, dispIdx, language, languages, setLanguage,
      langFlagInner, langName, langMenuOpen, langPick,
      picker, openPicker, closePicker, savePicker, clearPickerOverride, pickerMaterials,
      pickerDb, pickerVendors, currentSubtypes,
      pickerHasRfid, pickerHasOverride, pickerRfidStyle, readPickerRfid,
      cmdQueue, visibleQueue, cmdPaused, removeFromQueue, pauseQueue, resumeQueue, clearAllErrors,
      sendingAll, sendAllToPrinter,
      fmtArgs, cmdLabel,
      uploading, uploadInput, triggerUpload, onUploadGcode,
      railCollapsed, toggleRail, railItems, railSegStyle,
      secOpen, toggleSec, infoShown, toggleInfo, laneChips, appliedSnapshot,
      printCtl, printCtlLimits, printCtlDraft, printCtlBusy, printCtlError,
      printCtlSlide, printCtlRelease, printCtlReset, printCtlCancel,
      sendPrintControl,
      history, loadHistory, openHistoryDetail, historyColors,
      historyAccuracy, reprintJob,
      queue, loadQueue, launchQueued, deleteQueued, queueDriftTooltip,
      webcam, webcamShown, webcamSrc, webcamVideo, loadWebcam, startWebrtc,
      consoleLines, consoleFollow, consoleInput, consoleBusy, consoleEl,
      consoleShown, sendConsole, consoleTime, loadConsole,
      retryState, retryBusy, retryPct, retryControl,
      aceStartup, aceRescanBusy, aceRescan,
      firmware, firmwareWarn,
      applyModal, closeApplyModal, applyRestart, restartLabel,
      restartFirmware, restartPrinter,
      debugPanel, simulateEvent, debugSince,
      sampleBusy, loadSampleGcode,
      startInboxPreflight, dismissInbox, inboxCanStart,
    };
  },
}).mount("#app");
