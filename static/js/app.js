// CryFi — UI state, tab routing, and module wiring.
(function () {
  "use strict";

  let scanPollTimer = null;
  let scanPollMs = 2000;
  let selectedTarget = null;
  let _apsCache = []; // last rendered AP list (for highlight refresh)
  let regChannels = {}; // per-channel regulatory map (channel -> {tx_ok,...})
  let regInfo = null;   // {country, self_managed, tx_blocked_channels}

  // Capture/deauth workflow state
  let captureActive = false;
  let captureJobId = null;
  let captureTargetSnap = null; // frozen target while capturing
  let capturePollTimer = null;
  let captureStartMs = 0;
  let revealMode = false;       // true when listening to uncover a hidden SSID
  let revealedDone = false;
  let pendingCrackPrefill = null; // {cap, bssid} queued from Handshakes tab
  const runningJobs = new Set(); // every job_id we spawn (capture + deauths)
  let deauthSeq = 0; // running count for deauth toast labels

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // Stacking, self-dismissing toast cards. Each call spawns its own card, so
  // hitting Deauth twice shows two independent notifications.
  function toast(msg, kind, ttl) {
    const c = $("#toast-container");
    if (!c) return;
    // Back-compat: a truthy 2nd arg used to mean "isError".
    const cls = kind === true || kind === "err" ? "toast-err"
      : kind === "info" ? "toast-info" : "toast-ok";
    const el = document.createElement("div");
    el.className = "toast " + cls;
    el.textContent = msg;
    c.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    const remove = () => { el.classList.remove("show"); setTimeout(() => el.remove(), 200); };
    const timer = setTimeout(remove, ttl || 5000);
    el.addEventListener("click", () => { clearTimeout(timer); remove(); });
  }

  // The header pill is a persistent connection-status indicator (not a toast).
  function setHealth(text, ok) {
    const pill = $("#health-pill");
    if (!pill) return;
    pill.textContent = text;
    pill.className = "text-xs px-2 py-1 rounded-full " + (
      ok === false ? "bg-red-900/60 text-red-300"
      : ok ? "bg-emerald-900/60 text-emerald-300"
      : "bg-slate-800 text-slate-400");
  }

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function fmtTime(epoch) {
    return new Date(epoch * 1000).toLocaleString();
  }

  // --- Collapsible sections (single-page accordion) -----------------------
  const COLLAPSE_KEY = "na-collapsed:";

  function setCollapsed(sec, collapsed) {
    sec.classList.toggle("collapsed", collapsed);
    try { localStorage.setItem(COLLAPSE_KEY + sec.dataset.panel, collapsed ? "1" : "0"); } catch (_) {}
  }

  // Wrap each section's content into a collapsible body and make the header a
  // toggle. Done in JS so the markup stays simple.
  function setupCollapsibles() {
    $$(".section-card").forEach((sec) => {
      const head = sec.querySelector(".section-head");
      if (!head || sec.querySelector(".section-body")) return;
      // Move everything after the header into a body wrapper.
      const body = document.createElement("div");
      body.className = "section-body";
      let node = head.nextSibling;
      while (node) { const next = node.nextSibling; body.appendChild(node); node = next; }
      sec.appendChild(body);
      // Spacer pushes the chevron to the far right (works with/without actions).
      const spacer = document.createElement("span");
      spacer.className = "section-spacer";
      const chev = document.createElement("span");
      chev.className = "chevron";
      chev.textContent = "▾";
      head.append(spacer, chev);
      head.classList.add("section-head--toggle");
      // Toggle on header click — but not when an actual control was clicked.
      head.addEventListener("click", (e) => {
        if (e.target.closest("button, a, input, select, label")) return;
        setCollapsed(sec, !sec.classList.contains("collapsed"));
      });
      // Restore persisted state (default: expanded).
      try {
        if (localStorage.getItem(COLLAPSE_KEY + sec.dataset.panel) === "1") sec.classList.add("collapsed");
      } catch (_) {}
    });
    setupToggleAll();
  }

  function setupToggleAll() {
    const nav = $("#tabs");
    if (!nav || $("#toggle-all")) return;
    const btn = document.createElement("button");
    btn.id = "toggle-all";
    btn.className = "tab-btn ml-auto";
    const sync = () => {
      const anyOpen = $$(".section-card").some((s) => !s.classList.contains("collapsed"));
      btn.textContent = anyOpen ? "▾ Collapse all" : "▸ Expand all";
    };
    btn.addEventListener("click", () => {
      const anyOpen = $$(".section-card").some((s) => !s.classList.contains("collapsed"));
      $$(".section-card").forEach((s) => setCollapsed(s, anyOpen));
      sync();
    });
    nav.appendChild(btn);
    sync();
  }

  // --- Section navigation (single-page) -----------------------------------
  // Smooth-scroll to a section, expanding it first if collapsed.
  function goToSection(name) {
    const el = document.getElementById("sec-" + name);
    if (!el) return;
    setCollapsed(el, false);
    const ta = $("#toggle-all"); if (ta) ta.textContent = "▾ Collapse all";
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Highlight the nav button for whichever section is currently in view.
  function setupSectionSpy() {
    const sections = $$(".section-card");
    if (!("IntersectionObserver" in window) || !sections.length) return;
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        const name = en.target.dataset.panel;
        $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
      });
    }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });
    sections.forEach((s) => obs.observe(s));
  }

  // --- Interfaces ---------------------------------------------------------
  async function loadInterfaces() {
    const box = $("#iface-list");
    box.innerHTML = '<p class="text-slate-500 text-sm">Loading…</p>';
    try {
      const { interfaces } = await API.interfaces();
      populateIfaceSelect(interfaces);
      if (!interfaces.length) {
        box.innerHTML = '<p class="text-slate-500 text-sm">No wireless interfaces found.</p>';
        return;
      }
      box.innerHTML = "";
      interfaces.forEach((iface) => box.appendChild(ifaceCard(iface)));
    } catch (e) {
      box.innerHTML = `<p class="text-red-400 text-sm">${e.message}</p>`;
    }
  }

  function ifaceCard(iface) {
    const el = document.createElement("div");
    el.className = "iface-card";
    const mon = iface.monitor;
    const bus = (iface.bus || "").toUpperCase();
    const conn = [bus && (bus === "USB" ? "🔌 USB" : bus === "PCI" ? "💻 PCI" : bus), iface.driver]
      .filter(Boolean).join(" · ") || "—";

    // Detail rows (only show what's relevant).
    const rows = [];
    rows.push(["Connection", conn]);
    if (!mon && iface.ipv4) rows.push(["IPv4", iface.ipv4]);
    if (!mon && iface.ssid) rows.push(["Wi-Fi SSID", iface.ssid]);
    if (iface.mac) rows.push(["MAC", iface.mac]);
    const detailHtml = rows.map(([k, v]) =>
      `<div class="iface-kv"><span>${k}</span><code>${escapeHtml(v)}</code></div>`).join("");

    el.innerHTML = `
      <div class="iface-head">
        <div class="iface-name">${escapeHtml(iface.name)}</div>
        <span class="iface-badge ${mon ? "iface-monitor" : "iface-managed"}">
          ${mon ? "● Monitor" : "● Managed"}
        </span>
        <button class="${mon ? "btn-secondary" : "btn-primary"} ml-auto iface-toggle">
          ${mon ? "Stop Monitor" : "Start Monitor"}
        </button>
      </div>
      <div class="iface-details">${detailHtml}</div>`;

    el.querySelector(".iface-toggle").addEventListener("click", async (ev) => {
      ev.target.disabled = true;
      ev.target.textContent = mon ? "Stopping…" : "Starting…";
      try {
        if (mon) await API.monitorStop(iface.name);
        else await API.monitorStart(iface.name);
        toast(`${iface.name}: ${mon ? "monitor stopped" : "monitor enabled"}`, "info");
        await loadInterfaces();
      } catch (e) {
        toast(e.message, true);
        ev.target.disabled = false;
      }
    });
    return el;
  }

  function populateIfaceSelect(interfaces) {
    // Only monitor-mode interfaces can drive a scan/capture.
    const monitors = interfaces.filter((i) => i.monitor);
    const sel = $("#scan-iface");
    const prev = sel.value;
    sel.innerHTML = "";
    monitors.forEach((i) => {
      const o = document.createElement("option");
      o.value = i.name;
      o.textContent = i.name;
      sel.appendChild(o);
    });
    if (prev && monitors.some((i) => i.name === prev)) sel.value = prev;

    const none = monitors.length === 0;
    $("#scan-no-monitor").classList.toggle("hidden", !none);
    $("#scan-start").disabled = none;
  }

  // --- Scan ---------------------------------------------------------------
  async function startScan() {
    const iface = $("#scan-iface").value;
    if (!iface) { toast("Select a monitor interface first", true); return; }
    const payload = {
      iface,
      channel: $("#scan-channel").value.trim() || null,
      band: $("#scan-band").value.trim() || null,
    };
    try {
      const res = await API.scanStart(payload);
      scanPollMs = res.poll_ms || 2000;
      $("#scan-start").classList.add("hidden");
      $("#scan-stop").classList.remove("hidden");
      $("#scan-status").textContent = "scanning…";
      beginScanPoll();
    } catch (e) { toast(e.message, true); }
  }

  async function stopScan() {
    // Reset the UI optimistically so the button feels instant, then confirm.
    endScanPoll();
    $("#scan-start").classList.remove("hidden");
    $("#scan-start").disabled = false;
    $("#scan-stop").classList.add("hidden");
    $("#scan-status").textContent = "stopping…";
    try {
      await API.scanStop();
      $("#scan-status").textContent = "stopped";
    } catch (e) {
      $("#scan-status").textContent = "stop failed: " + e.message;
    }
  }

  function beginScanPoll() {
    endScanPoll();
    const tick = async () => {
      try {
        const data = await API.scanResults();
        renderScanRows(data.aps);
        if (!data.running) {
          $("#scan-status").textContent = "scan ended";
          $("#scan-start").classList.remove("hidden");
          $("#scan-stop").classList.add("hidden");
          endScanPoll();
        }
      } catch (e) { /* keep polling */ }
    };
    tick();
    scanPollTimer = setInterval(tick, scanPollMs);
  }
  function endScanPoll() {
    if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; }
  }

  function renderScanRows(aps) {
    _apsCache = aps;
    const tbody = $("#scan-rows");
    const empty = $("#scan-empty");
    aps.sort((a, b) => b.power - a.power);
    if (!aps.length) { empty.classList.remove("hidden"); tbody.innerHTML = ""; return; }
    empty.classList.add("hidden");
    tbody.innerHTML = "";
    aps.forEach((ap) => {
      const tr = document.createElement("tr");
      tr.className = "scan-row";
      if (selectedTarget && selectedTarget.bssid === ap.bssid) tr.classList.add("selected");
      const essidCell = ap.hidden
        ? '<span class="text-slate-500">&lt;hidden&gt;</span> <span class="essid-hidden" title="Hidden network — select it to reveal">🔒</span>'
        : escapeHtml(ap.essid);
      tr.innerHTML = `
        <td><code>${ap.bssid}</code></td>
        <td>${essidCell}</td>
        <td>${ap.channel}</td>
        <td>${signalCell(ap.power)}</td>
        <td>${encLabel(ap)}</td>
        <td>${ap.data}</td>`;
      tr.addEventListener("click", () => selectTarget(ap));
      tbody.appendChild(tr);
    });
  }

  // Signal strength: airodump power is dBm (closer to 0 = stronger; -1 = n/a).
  // Render colored bars + a plain-language label so it's readable at a glance.
  function signalCell(power) {
    const p = Number(power);
    if (!p || p === -1 || p <= -90) return '<span class="sig-na">—</span>';
    let lvl, cls, label;
    if (p >= -55) { lvl = 4; cls = "sig-green"; label = "Excellent"; }
    else if (p >= -67) { lvl = 3; cls = "sig-green"; label = "Good"; }
    else if (p >= -75) { lvl = 2; cls = "sig-amber"; label = "Fair"; }
    else { lvl = 1; cls = "sig-red"; label = "Weak"; }
    return `<span class="sig lvl${lvl} ${cls}" title="${label} · ${p} dBm">`
      + "<i></i><i></i><i></i><i></i></span>"
      + `<span class="sig-dbm">${label}</span>`;
  }

  // Show encryption with a crackability hint: WPA3/SAE is amber (hard),
  // WPA2-PSK green (crackable), open/WEP noted.
  function encLabel(ap) {
    const p = (ap.privacy || "").toUpperCase();
    const a = (ap.auth || "").toUpperCase();
    if (p.includes("WPA3") || a.includes("SAE")) {
      const transition = p.includes("WPA2");
      return `<span class="enc enc-wpa3" title="${transition ? "WPA3/WPA2 transition — only a WPA2 client's handshake is crackable" : "WPA3-SAE — not crackable offline"}">WPA3${transition ? "/2" : ""}</span>`;
    }
    if (p.includes("WPA2")) return '<span class="enc enc-wpa2" title="WPA2-PSK — crackable with a 4-way handshake">WPA2</span>';
    if (p.includes("WPA")) return '<span class="enc enc-wpa2">WPA</span>';
    if (p.includes("WEP")) return '<span class="enc enc-wpa2">WEP</span>';
    if (!p || p.includes("OPN")) return '<span class="enc enc-open">open</span>';
    return escapeHtml(ap.privacy);
  }

  function selectTarget(ap) {
    // While a capture is locked to a target, other networks can't be selected.
    if (captureActive) {
      toast("Stop the capture to select another network", true);
      return;
    }
    selectedTarget = ap;
    $("#t-bssid").textContent = ap.bssid;
    $("#t-essid").textContent = ap.hidden ? "🔒 hidden" : (ap.essid || "<hidden>");
    $("#t-channel").textContent = ap.channel;
    $("#t-enc").textContent = ap.privacy || "—";
    // Offer "Reveal SSID" only for hidden networks.
    $("#t-reveal").classList.toggle("hidden", !ap.hidden);
    // Highlight the row without a full re-render.
    $$(".scan-row").forEach((r) => r.classList.remove("selected"));
    [...$$(".scan-row")].find((r) => r.querySelector("code")?.textContent === ap.bssid)
      ?.classList.add("selected");
    setCaptureUiState();
  }

  // --- Capture / Deauth state machine ------------------------------------
  // captureActive locks target selection; deauth is only enabled while active.

  function setCaptureUiState() {
    const hasTarget = !!selectedTarget;
    $("#t-capture").classList.toggle("hidden", captureActive);
    $("#t-capture").disabled = !hasTarget || captureActive;
    $("#t-stop-capture").classList.toggle("hidden", !captureActive);
    // Reveal button: only for a hidden target, and not while a job is running.
    $("#t-reveal").classList.toggle("hidden", captureActive || !(hasTarget && selectedTarget.hidden));
    // Deauth only makes sense while a capture is running on this target.
    $("#t-deauth").disabled = !captureActive;
    $("#scan-rows").classList.toggle("locked", captureActive);
  }

  function captureStatus(msg, tone) {
    const el = $("#t-capture-status");
    el.textContent = msg || "";
    el.className = "text-[11px] min-h-[1rem] " + (
      tone === "ok" ? "text-emerald-400"
      : tone === "warn" ? "text-amber-400"
      : "text-slate-400");
  }

  async function captureTarget() {
    if (!selectedTarget || captureActive) return;
    const iface = $("#scan-iface").value;
    if (!iface) { toast("No monitor interface selected", true); return; }
    try {
      const res = await API.capture({
        iface,
        bssid: selectedTarget.bssid,
        channel: String(selectedTarget.channel),
        essid: selectedTarget.essid || "",
      });
      captureJobId = res.job_id;
      runningJobs.add(res.job_id);
      captureActive = true;
      deauthSeq = 0;
      captureStartMs = Date.now();
      captureTargetSnap = { ...selectedTarget, iface };
      setCaptureUiState();
      revealedDone = false;
      captureStatus(revealMode
        ? "🔓 Listening to reveal the hidden SSID — deauth a client to speed it up…"
        : "📡 Capturing — waiting for handshake…", "warn");
      // Parsed dashboard + a clean airodump-style live table (built from the
      // CSV) — airodump runs with --background so it doesn't flood a raw stream.
      showCaptureDash(captureTargetSnap);
      $("#capture-out").textContent = "Waiting for airodump data…";
      beginCapturePoll();
      toast(`${revealMode ? "Reveal" : "Capture"} started → ${captureTargetSnap.bssid}`, "info");
      if (txBlocked(captureTargetSnap.channel)) {
        const ch = captureTargetSnap.channel, why = txBlockReason(ch);
        captureStatus(`Ch ${ch}: ${why} — deauth won't go out; wait for a natural reconnect.`, "warn");
        toast(`Channel ${ch} is ${why} in regdomain ${regInfo ? regInfo.country : "?"} — deauth blocked. Listening passively; the 2.4 GHz twin usually shares the password.`, "info", 9000);
      }
    } catch (e) { toast(e.message, true); captureStatus(e.message, "warn"); }
  }

  // 5 GHz channels (≥ 36) are frequently no-IR / DFS — fallback heuristic only.
  function is5GHz(ch) { return Number(ch) >= 36; }

  // --- Regulatory awareness -----------------------------------------------
  async function loadRegulatory() {
    try {
      const r = await API.regulatory();
      regInfo = r;
      regChannels = r.channels || {};
      const blocked = r.tx_blocked_channels || [];
      const sm = r.self_managed ? " · self-managed" : "";
      const summary = blocked.length > 12
        ? `${blocked.length} channels (mostly 5 GHz DFS/no-IR)`
        : blocked.join(", ");
      const el = $("#reg-info");
      el.innerHTML = blocked.length
        ? `📍 Regulatory domain <b>${escapeHtml(r.country)}</b>${sm}: deauth (transmit) blocked on <b title="${blocked.join(", ")}">${summary}</b> — scanning/capture still works there (passive).`
        : `📍 Regulatory domain <b>${escapeHtml(r.country)}</b>${sm}: transmit allowed on all detected channels.`;
      el.classList.remove("hidden");
    } catch (_) { /* iw not available — fall back to the heuristic */ }
  }

  // True if the regdomain forbids transmitting (deauth) on this channel.
  function txBlocked(ch) {
    const c = regChannels[String(ch)];
    if (c) return !c.tx_ok;
    return is5GHz(ch);  // heuristic fallback when reg data is unavailable
  }
  function txBlockReason(ch) {
    const c = regChannels[String(ch)];
    return (c && c.reason) ? c.reason : "regulatory restriction";
  }

  // --- Capture dashboard (parsed progress under Target Properties) --------
  function showCaptureDash(t) {
    $("#cap-target").textContent = `${t.essid || "<hidden>"} · ${t.bssid} · ch ${t.channel}`;
    setCapState("capturing");
    $("#cap-data").textContent = "0";
    $("#cap-beacons").textContent = "0";
    $("#cap-clients").textContent = "0";
    $("#cap-elapsed").textContent = "0s";
    $("#cap-clients-list").innerHTML = "";
    const f = $("#cap-found"); f.classList.add("hidden"); f.classList.remove("show"); f.innerHTML = "";
    $("#capture-dash").classList.remove("hidden");
  }

  function setCapState(state) {
    const el = $("#cap-state");
    el.textContent = state;
    el.className = "crack-state " + (["captured", "revealed"].includes(state) ? "found"
      : state === "capturing" ? "capturing"
      : "notfound");
  }

  function renderCaptureDash(st) {
    if (!st) return;
    $("#cap-data").textContent = Number(st.data || 0).toLocaleString();
    $("#cap-beacons").textContent = Number(st.beacons || 0).toLocaleString();
    const clients = st.clients || [];
    $("#cap-clients").textContent = clients.length;
    $("#cap-clients-list").innerHTML = clients
      .map((c) => `<span class="cap-client" title="${c.power} dBm · ${c.packets} pkts">${escapeHtml(c.station)}</span>`)
      .join("");
    $("#cap-elapsed").textContent = Math.round((Date.now() - captureStartMs) / 1000) + "s";
    renderCaptureTable(st);
  }

  // Clean airodump-style table, rebuilt in place each poll (no raw flood).
  function renderCaptureTable(st) {
    const pad = (s, n) => String(s ?? "").padEnd(n).slice(0, n);
    const essid = st.revealed_essid || st.essid || "<hidden>";
    const rows = [
      pad("BSSID", 19) + pad("PWR", 5) + pad("Beacons", 9) + pad("#Data", 8) + pad("CH", 4) + "ESSID",
      pad(st.bssid, 19) + pad("", 5) + pad(st.beacons || 0, 9) + pad(st.data || 0, 8) + pad(st.channel, 4) + essid,
      "",
      pad("STATION", 19) + pad("PWR", 6) + "Packets",
    ];
    (st.clients || []).forEach((c) =>
      rows.push(pad(c.station, 19) + pad(c.power, 6) + (c.packets ?? "")));
    if (!(st.clients || []).length) rows.push("(no clients seen yet)");
    $("#capture-out").textContent = rows.join("\n");
  }

  // Reset all capture/deauth UI to idle. Synchronous — never awaits the network.
  function resetCaptureUi() {
    endCapturePoll();
    captureActive = false;
    captureJobId = null;
    if (!revealedDone) revealMode = false;  // clear unless a reveal just succeeded
    setCaptureUiState();
  }

  // Fire-and-forget kill of a batch of job ids (capture + any deauths).
  function killJobs(ids) {
    const arr = [...new Set(ids)].filter(Boolean);
    if (!arr.length) return Promise.resolve(0);
    return Promise.allSettled(arr.map((id) => API.jobStop(id))).then(() => arr.length);
  }

  // User pressed Stop — kill EVERYTHING (capture + all deauths) and reset the
  // UI immediately, then confirm the kills in the background.
  function stopAll() {
    const ids = [...runningJobs, captureJobId];
    runningJobs.clear();
    resetCaptureUi();
    captureStatus("Stopped.", null);
    killJobs(ids).then((n) => { if (n) toast(`Stopped ${n} job${n === 1 ? "" : "s"}`, "info"); });
  }

  // Capture ended on its own (handshake captured, or process exited).
  function endCapture(handshakeOk) {
    const leftovers = [...runningJobs];
    runningJobs.clear();
    resetCaptureUi();
    if (handshakeOk) {
      setCapState("captured");
      captureStatus("✓ Handshake captured! Added to section 4.", "ok");
      const f = $("#cap-found");
      f.innerHTML = '<div class="big">🎉🤝 Handshake captured!</div>' +
        '<div style="margin-top:.3rem;font-size:.85rem">Saved — pick it in section 4 or 6 to crack.</div>';
      f.classList.remove("hidden"); f.classList.remove("show"); void f.offsetWidth; f.classList.add("show");
      loadHandshakes();
    } else {
      setCapState("stopped");
    }
    killJobs(leftovers); // stop any deauths still firing
  }

  function beginCapturePoll() {
    endCapturePoll();
    let misses = 0;
    const jobId = captureJobId; // capture for this poll's lifetime
    capturePollTimer = setInterval(async () => {
      if (!captureActive || captureJobId !== jobId) return;
      try {
        const st = await API.captureJobStatus(jobId);
        misses = 0;
        renderCaptureDash(st);
        // Hidden-SSID revealed: airodump filled in the name.
        if (revealMode && !revealedDone && st.revealed_essid) {
          revealSucceeded(st.revealed_essid);
          return;
        }
        if (st.done || st.handshake_captured) {
          const ok = !!st.handshake_captured;
          endCapture(ok);
          if (ok) toast("✓ Handshake captured", "ok");
          else { captureStatus("Capture ended (no handshake).", "warn"); toast("Capture ended — no handshake", "info"); }
        }
      } catch (_) {
        if (++misses >= 5) { endCapture(false); captureStatus("Lost track of capture job.", "warn"); }
      }
    }, 1500);
  }
  function endCapturePoll() {
    if (capturePollTimer) { clearInterval(capturePollTimer); capturePollTimer = null; }
  }

  // --- Reveal hidden SSID -------------------------------------------------
  function revealTarget() {
    if (!selectedTarget || captureActive) return;
    revealMode = true;
    captureTarget();  // reuse the listen/dashboard/deauth machinery
  }

  function revealSucceeded(name) {
    revealedDone = true;
    revealMode = false;  // from here it's a normal capture — keep going
    const bssid = (captureTargetSnap || selectedTarget || {}).bssid;
    // Surface the revealed name, but DO NOT stop — let the capture keep running
    // so a handshake (forced via deauth) is still caught and shown live.
    captureStatus(`🔓 Revealed: ${name} — still capturing; deauth a client for the handshake…`, "ok");
    toast(`🔓 Hidden SSID revealed: ${name}`, "info", 10000);
    $("#cap-target").textContent = `${name} · ${bssid} · ch ${(captureTargetSnap || {}).channel ?? "?"}`;
    // Reflect the name in the Target panel + the scan table row + cache.
    $("#t-essid").textContent = name;
    if (selectedTarget) { selectedTarget.essid = name; selectedTarget.hidden = false; }
    if (captureTargetSnap) captureTargetSnap.essid = name;
    const cached = _apsCache.find((a) => a.bssid === bssid);
    if (cached) { cached.essid = name; cached.hidden = false; }
    if (bssid) {
      const row = [...$$(".scan-row")].find((r) => r.querySelector("code")?.textContent === bssid);
      if (row && row.children[1]) row.children[1].textContent = name;
    }
  }

  // Fire-and-forget: give instant feedback and never block the button on the
  // network round-trip, so rapid repeat-deauths each pop their own toast.
  function deauthTarget() {
    const t = captureTargetSnap || selectedTarget;
    if (!t) { toast("Select a target first", true); return; }
    if (!captureActive) { toast("Start a capture before deauthing", true); return; }
    const iface = t.iface || $("#scan-iface").value;
    const count = parseInt($("#t-deauth-count").value || "5", 10);
    const n = ++deauthSeq;
    const label = count === 0 ? "continuous" : `${count} burst${count === 1 ? "" : "s"}`;
    // Toast immediately — independent of when the backend confirms the spawn.
    toast(`Deauth #${n} → ${t.bssid} · ${label}`, "info");
    captureStatus(`📡 Capturing… ${n} deauth(s) sent, waiting for handshake…`, "warn");
    if (txBlocked(t.channel)) {
      toast(`⚠️ Channel ${t.channel}: ${txBlockReason(t.channel)} (regdomain ${regInfo ? regInfo.country : "?"}) — deauth frames likely won't transmit. Try the 2.4 GHz SSID.`, true, 9000);
    }
    API.deauth({ iface, bssid: t.bssid, channel: String(t.channel), count })
      .then((res) => { if (res && res.job_id) runningJobs.add(res.job_id); })
      .catch((e) => toast(`Deauth #${n} failed: ${e.message}`, true));
  }

  // --- Files --------------------------------------------------------------
  // name -> "yes" | "no" handshake status from the last Analyze run.
  let capHandshakeStatus = {};

  async function loadCaptures() {
    const box = $("#cap-list");
    box.innerHTML = '<p class="text-slate-500 text-sm">Loading…</p>';
    try {
      const { captures } = await API.captures();
      $("#cap-select-all").checked = false;
      if (!captures.length) { box.innerHTML = '<p class="text-slate-500 text-sm">No capture files yet.</p>'; return; }
      box.innerHTML = "";
      captures.forEach((f) => box.appendChild(captureRow(f)));
    } catch (e) { box.innerHTML = `<p class="text-red-400 text-sm">${e.message}</p>`; }
  }

  function capIsPacket(name) { return /\.(cap|pcap)$/i.test(name); }

  function hsBadge(name) {
    if (!capIsPacket(name)) return "";
    const s = capHandshakeStatus[name];
    if (s === "yes") return '<span class="cap-badge cap-hs-yes">✓ handshake</span>';
    if (s === "no") return '<span class="cap-badge cap-hs-no">✗ none</span>';
    return '<span class="cap-badge cap-hs-unknown">? unchecked</span>';
  }

  function captureRow(f) {
    const el = document.createElement("div");
    el.className = "file-row";
    el.innerHTML = `
      <div class="flex items-center gap-2 min-w-0">
        <input type="checkbox" class="cap-sel accent-emerald-500" data-name="${escapeHtml(f.name)}">
        <div class="min-w-0">
          <div class="truncate">${escapeHtml(f.name)} ${hsBadge(f.name)}</div>
          <div class="meta">${fmtBytes(f.size)} · ${fmtTime(f.mtime)}</div>
        </div>
      </div>
      <div class="flex gap-2 shrink-0">
        <a class="btn-secondary" href="/api/captures/${encodeURIComponent(f.name)}/download">Download</a>
        <button class="btn-danger">Delete</button>
      </div>`;
    el.querySelector("button").addEventListener("click", async () => {
      if (!confirm(`Delete ${f.name} (and its sibling .csv/.netxml)?`)) return;
      try { await API.deleteCapture(f.name); loadCaptures(); } catch (e) { toast(e.message, true); }
    });
    return el;
  }

  function selectedCaptures() {
    return $$(".cap-sel:checked").map((c) => c.dataset.name);
  }

  async function analyzeCaptures() {
    const btn = $("#cap-analyze"); const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Analyzing…";
    try {
      const { results } = await API.analyzeCaptures();
      capHandshakeStatus = {};
      results.forEach((r) => { capHandshakeStatus[r.name] = r.has_handshake ? "yes" : "no"; });
      const withHs = results.filter((r) => r.has_handshake).length;
      toast(`Analyzed ${results.length} capture(s): ${withHs} with handshake`, "info");
      loadCaptures();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  async function cleanCaptures() {
    if (!confirm("Delete every capture (and siblings) that has NO WPA handshake?")) return;
    const btn = $("#cap-clean"); const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Cleaning…";
    try {
      const r = await API.cleanCaptures();
      toast(`Cleaned ${r.removed_count} file(s); kept ${r.kept_with_handshake.length} with handshake`, "info");
      loadCaptures(); loadCrackOptions();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  async function deleteSelectedCaptures() {
    const names = selectedCaptures();
    if (!names.length) { toast("No files selected", true); return; }
    if (!confirm(`Delete ${names.length} selected file(s) (and siblings)?`)) return;
    try {
      const r = await API.deleteCaptures(names);
      toast(`Deleted ${r.count} file(s)`, "info");
      loadCaptures(); loadCrackOptions();
    } catch (e) { toast(e.message, true); }
  }

  async function deleteAllCaptures() {
    const names = $$(".cap-sel").map((c) => c.dataset.name);
    if (!names.length) { toast("No capture files", true); return; }
    if (!confirm(`Delete ALL ${names.length} capture file(s)? This cannot be undone.`)) return;
    try {
      const r = await API.deleteCaptures(names);
      toast(`Deleted ${r.count} file(s)`, "info");
      loadCaptures(); loadCrackOptions();
    } catch (e) { toast(e.message, true); }
  }

  async function loadWordlists() {
    const box = $("#wl-list");
    box.innerHTML = '<p class="text-slate-500 text-sm">Loading…</p>';
    try {
      const { wordlists } = await API.wordlists();
      if (!wordlists.length) { box.innerHTML = '<p class="text-slate-500 text-sm">No wordlists uploaded.</p>'; return; }
      box.innerHTML = "";
      wordlists.forEach((f) => box.appendChild(wordlistRow(f)));
    } catch (e) { box.innerHTML = `<p class="text-red-400 text-sm">${e.message}</p>`; }
  }

  function wordlistRow(f) {
    const el = document.createElement("div");
    el.className = "wl-item";
    const lines = (f.lines != null) ? Number(f.lines).toLocaleString() : "?";
    el.innerHTML = `
      <div class="file-row">
        <div class="min-w-0">
          <div class="truncate">${escapeHtml(f.name)}</div>
          <div class="meta">${lines} lines · ${fmtBytes(f.size)}</div>
        </div>
        <div class="flex gap-2 shrink-0">
          <button class="btn-secondary wl-preview">Preview</button>
          <button class="btn-danger wl-del">Delete</button>
        </div>
      </div>
      <pre class="wl-preview-box terminal hidden"></pre>`;

    const box = el.querySelector(".wl-preview-box");
    el.querySelector(".wl-preview").addEventListener("click", async (ev) => {
      if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
      ev.target.textContent = "…";
      try {
        const { sample } = await API.previewWordlist(f.name, 10);
        box.textContent = sample.length ? sample.join("\n") : "(empty)";
        box.classList.remove("hidden");
      } catch (e) { toast(e.message, true); }
      finally { ev.target.textContent = "Preview"; }
    });
    el.querySelector(".wl-del").addEventListener("click", async () => {
      if (!confirm(`Delete ${f.name}?`)) return;
      try { await API.deleteWordlist(f.name); loadWordlists(); loadCrackOptions(); } catch (e) { toast(e.message, true); }
    });
    return el;
  }

  async function uploadWordlist(ev) {
    ev.preventDefault();
    const input = $("#wl-file");
    if (!input.files.length) { toast("Choose a .txt file", true); return; }
    $("#wl-status").textContent = "uploading…";
    try {
      await API.uploadWordlist(input.files[0]);
      $("#wl-status").textContent = "uploaded";
      input.value = "";
      loadWordlists();
    } catch (e) { $("#wl-status").textContent = ""; toast(e.message, true); }
  }

  // --- Handshakes ---------------------------------------------------------
  async function loadHandshakes() {
    const box = $("#hs-list");
    box.innerHTML = '<p class="text-slate-500 text-sm">Loading…</p>';
    try {
      const { handshakes } = await API.handshakes();
      updateHsBadge(handshakes.length);
      if (!handshakes.length) {
        box.innerHTML = '<p class="text-slate-500 text-sm">No handshakes captured yet. Capture one from the Scan tab.</p>';
        return;
      }
      box.innerHTML = "";
      handshakes.forEach((h) => box.appendChild(hsCard(h)));
    } catch (e) { box.innerHTML = `<p class="text-red-400 text-sm">${e.message}</p>`; }
  }

  function updateHsBadge(n) {
    const badge = $("#hs-badge");
    badge.textContent = n;
    badge.classList.toggle("hidden", !n);
  }

  function hsCard(h) {
    const el = document.createElement("div");
    el.className = "hs-card";
    const hidden = !h.essid;
    const essidHtml = hidden
      ? '<span class="text-slate-500">&lt;hidden&gt;</span> <span class="essid-hidden">🔒</span>'
      : escapeHtml(h.essid);
    const cracked = !!h.password;
    const passRow = cracked
      ? `<div class="hs-pass">🔑 password:
           <code class="hs-pass-val" data-pw="${escapeHtml(h.password)}">••••••••</code>
           <button class="btn-secondary hs-pass-reveal">Reveal</button>
           <button class="btn-secondary hs-pass-copy">Copy</button>
         </div>`
      : "";
    el.innerHTML = `
      <div class="min-w-0">
        <div class="essid">${essidHtml} ${cracked ? '<span class="hs-cracked">✓ cracked</span>' : ""}</div>
        <div class="meta">
          <code>${h.bssid}</code> · ch ${h.channel ?? "?"} · ${escapeHtml(h.cap)}
          ${h.captured_at ? "· " + escapeHtml(h.captured_at) : ""}
        </div>
        ${passRow}
      </div>
      <div class="flex gap-2 shrink-0">
        ${hidden ? '<button class="btn-secondary hs-reveal" title="Recover the SSID from the handshake">🔓 Reveal SSID</button>' : ""}
        <button class="btn-primary hs-attack">${cracked ? "Re-crack" : "Dictionary Attack"}</button>
        <button class="btn-danger hs-del">Delete</button>
      </div>`;
    el.querySelector(".hs-attack").addEventListener("click", () => crackFromHandshake(h));
    el.querySelector(".hs-del").addEventListener("click", async () => {
      if (!confirm(`Delete handshake + capture ${h.cap}?`)) return;
      try { await API.deleteHandshake(h.cap); loadHandshakes(); } catch (e) { toast(e.message, true); }
    });
    const revealBtn = el.querySelector(".hs-reveal");
    if (revealBtn) revealBtn.addEventListener("click", async () => {
      revealBtn.disabled = true; revealBtn.textContent = "Revealing…";
      try {
        const r = await API.revealHandshakeSsid(h.cap);
        toast(`🔓 SSID revealed: ${r.essid}`, "info", 9000);
        loadHandshakes();
      } catch (e) {
        toast(e.message, true);
        revealBtn.disabled = false; revealBtn.textContent = "🔓 Reveal SSID";
      }
    });
    // Masked password reveal / copy.
    const pwVal = el.querySelector(".hs-pass-val");
    const pwReveal = el.querySelector(".hs-pass-reveal");
    if (pwReveal) {
      let shown = false;
      pwReveal.addEventListener("click", () => {
        shown = !shown;
        pwVal.textContent = shown ? pwVal.dataset.pw : "••••••••";
        pwReveal.textContent = shown ? "Hide" : "Reveal";
      });
    }
    const pwCopy = el.querySelector(".hs-pass-copy");
    if (pwCopy) pwCopy.addEventListener("click", () => {
      navigator.clipboard?.writeText(pwVal.dataset.pw).then(() => toast("Password copied", "info"));
    });
    return el;
  }

  function crackFromHandshake(h) {
    // Prefill the crack form with this handshake, then scroll to section 6.
    pendingCrackPrefill = { cap: h.cap, bssid: h.bssid };
    loadCrackOptions().then(() => goToSection("crack"));
  }

  // --- Crack --------------------------------------------------------------
  async function loadCrackOptions() {
    try {
      const [{ captures }, { wordlists }] = await Promise.all([API.captures(), API.wordlists()]);
      const capSel = $("#crack-cap");
      const caps = captures.filter((c) => /\.(cap|pcap)$/i.test(c.name));
      capSel.innerHTML = caps.length
        ? caps.map((c) => `<option>${escapeHtml(c.name)}</option>`).join("")
        : '<option value="">— no .cap files —</option>';
      const wlSel = $("#crack-wl");
      wlSel.innerHTML = wordlists.length
        ? wordlists.map((w) => `<option>${escapeHtml(w.name)}</option>`).join("")
        : '<option value="">— no wordlists —</option>';

      // Apply a pending prefill coming from the Handshakes tab.
      if (pendingCrackPrefill) {
        if ([...capSel.options].some((o) => o.value === pendingCrackPrefill.cap)) {
          capSel.value = pendingCrackPrefill.cap;
        }
        $("#crack-bssid").value = pendingCrackPrefill.bssid || "";
        if (!wordlists.length) toast("Upload a wordlist (Files tab) to run the attack", true);
        pendingCrackPrefill = null;
      }
    } catch (e) { toast(e.message, true); }
  }

  let crackJobId = null;
  let crackPollTimer = null;

  async function startCrack() {
    const cap = $("#crack-cap").value;
    const wl = $("#crack-wl").value;
    if (!cap || !wl) { toast("Select a capture and a wordlist", true); return; }
    const payload = { cap_file: cap, wordlist: wl };
    const bssid = $("#crack-bssid").value.trim();
    if (bssid) payload.bssid = bssid;
    try {
      const res = await API.crack(payload);
      crackJobId = res.job_id;
      // Reset + show the clean dashboard; raw output stays in the collapsible.
      crackResetDash(cap, wl);
      $("#crack-dash").classList.remove("hidden");
      $("#crack-stop").classList.remove("hidden");
      $("#crack-start").disabled = true;
      // Render a clean aircrack-style view from parsed status (in place), rather
      // than streaming the raw ANSI TUI which piles up on every redraw.
      $("#crack-out").textContent = "Starting aircrack-ng…";
      beginCrackPoll();
    } catch (e) { toast(e.message, true); }
  }

  function crackResetDash(cap, wl) {
    $("#crack-target").textContent = `${cap} · ${wl}`;
    setCrackState("running");
    $("#crack-bar").style.width = "0%";
    $("#crack-speed").textContent = "—";
    $("#crack-tested").textContent = "—";
    $("#crack-percent").textContent = "—";
    $("#crack-current").textContent = "…";
    const f = $("#crack-found"); f.classList.add("hidden"); f.classList.remove("show"); f.innerHTML = "";
  }

  function setCrackState(state) {
    const el = $("#crack-state");
    el.textContent = state;
    const cls = state === "found" ? "found"
      : state === "stopped" ? "stopped"
      : state === "running" ? "" : "notfound";  // notfound/wpa3/no handshake → red
    el.className = "crack-state " + cls;
  }

  function renderCrackDash(meta) {
    if (!meta) return;
    if (meta.speed != null) $("#crack-speed").textContent = Number(meta.speed).toLocaleString();
    if (meta.tested != null) {
      $("#crack-tested").textContent = Number(meta.tested).toLocaleString() +
        (meta.total ? " / " + Number(meta.total).toLocaleString() : "");
    }
    if (meta.percent != null) {
      $("#crack-percent").textContent = meta.percent + "%";
      $("#crack-bar").style.width = Math.min(100, meta.percent) + "%";
    }
    if (meta.current) $("#crack-current").textContent = meta.current;
    renderCrackText(meta);
  }

  // Clean aircrack-ng-style text view, rebuilt in place each poll.
  function renderCrackText(meta) {
    const t = meta.tested != null ? Number(meta.tested).toLocaleString() : "—";
    const tot = meta.total ? " / " + Number(meta.total).toLocaleString() : "";
    const spd = meta.speed != null ? Number(meta.speed).toLocaleString() : "—";
    const pct = meta.percent != null ? ` (${meta.percent}%)` : "";
    const rows = ["                        Aircrack-ng", ""];
    if (meta.status === "found" && meta.key) {
      rows.push(`      KEY FOUND! [ ${meta.key} ]`);
    } else {
      rows.push(`      [keys tested] ${t}${tot}${pct}`);
      rows.push(`      [speed]       ${spd} k/s`);
      rows.push("");
      rows.push(`      Current passphrase:  ${meta.current || "…"}`);
    }
    $("#crack-out").textContent = rows.join("\n");
  }

  function crackCelebrate(key) {
    const f = $("#crack-found");
    f.innerHTML = `<div class="big">🎉🔓 KEY FOUND!</div>
      <div style="margin-top:.4rem">passphrase: <code>${escapeHtml(key)}</code></div>
      <button id="crack-copy" class="btn-secondary" style="margin-top:.6rem">Copy</button>`;
    f.classList.remove("hidden");
    // restart pop animation
    f.classList.remove("show"); void f.offsetWidth; f.classList.add("show");
    $("#crack-copy").addEventListener("click", () => {
      navigator.clipboard?.writeText(key).then(() => toast("Passphrase copied", "info"));
    });
  }

  function endCrack(meta) {
    endCrackPoll();
    $("#crack-stop").classList.add("hidden");
    $("#crack-start").disabled = false;
    const status = meta && meta.status;
    if (status === "found" && meta.key) {
      setCrackState("found");
      $("#crack-current").textContent = meta.key;
      crackCelebrate(meta.key);
      toast(`🎉 Key found: ${meta.key}`, "info", 10000);
      loadHandshakes();  // password now stored — show it (masked) in section 3
    } else if (status === "stopped") {
      setCrackState("stopped");
    } else if (status === "wpa3") {
      setCrackState("wpa3");
      $("#crack-current").textContent = "—";
      $("#crack-found").classList.remove("hidden");
      $("#crack-found").innerHTML =
        '<div class="big">🛡️ WPA3 (SAE) capture</div>' +
        '<div style="margin-top:.4rem;font-size:.85rem">aircrack-ng can\'t crack SAE. If the network is WPA3/WPA2 <em>transition</em> mode, capture a <strong>WPA2 client\'s</strong> 4-way handshake instead (same password).</div>';
      toast("This is a WPA3/SAE handshake — not crackable. Capture a WPA2 client's handshake.", true, 11000);
    } else if (status === "no_handshake") {
      setCrackState("no handshake");
      $("#crack-current").textContent = "—";
      toast("No valid WPA2 handshake in this capture — re-capture (deauth a connected client).", true, 9000);
    } else {
      setCrackState("notfound");
      $("#crack-current").textContent = "—";
      toast("Passphrase not found in wordlist", true);
    }
  }

  function beginCrackPoll() {
    endCrackPoll();
    const jobId = crackJobId;
    let misses = 0;
    crackPollTimer = setInterval(async () => {
      if (crackJobId !== jobId) return;
      try {
        const st = await API.jobGet(jobId);
        misses = 0;
        renderCrackDash(st.meta);
        if (st.done) endCrack(st.meta);
      } catch (_) { if (++misses >= 5) endCrack(null); }
    }, 600);
  }
  function endCrackPoll() {
    if (crackPollTimer) { clearInterval(crackPollTimer); crackPollTimer = null; }
  }

  async function stopCrack() {
    if (!crackJobId) return;
    const id = crackJobId;
    endCrackPoll();
    $("#crack-stop").classList.add("hidden");
    $("#crack-start").disabled = false;
    setCrackState("stopped");
    try { await API.jobStop(id); } catch (_) {}
    toast("Crack stopped", "info");
  }

  // --- Wordlist Studio (section 7) — one unified generator ----------------
  function wgParseList(str) {
    return (str || "").split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  }
  const wgFmt = (n) => Number(n).toLocaleString();

  // Build the merged strategy list from the single form.
  function wgStrategies() {
    const strategies = [];
    // Core: target words (the unified words generator).
    const words = [
      ...$$(".wg-word").map((i) => i.value.trim()).filter(Boolean),
      ...wgParseList($("#wg-words").value),
    ];
    if (words.length) {
      strategies.push({
        mode: "words", words,
        years: wgParseList($("#wg-years").value),
        numbers: wgParseList($("#wg-numbers").value),
        suffixes: $("#wg-suffix").checked ? null : [],
        use_cases: $("#wg-cases").checked,
        use_leet: $("#wg-leet").checked,
        reverse: $("#wg-reverse").checked,
        append_numbers: $("#wg-append").checked,
        min_parts: parseInt($("#wg-min").value || "1", 10),
        max_parts: parseInt($("#wg-max").value || "2", 10),
        separators: ($("#wg-seps").value || "").split(",").map((s) => s.trim()),
      });
    }
    // Optional add-on: phone mask.
    if ($("#wg-en-phone").checked && $("#wg-phone-mask").value.trim()) {
      strategies.push({
        mode: "phone", mask: $("#wg-phone-mask").value.trim(), wildcard: "X",
        strip_nondigits: $("#wg-phone-strip").checked,
      });
    }
    // Optional add-on: charset brute-force.
    if ($("#wg-en-charset").checked) {
      strategies.push({
        mode: "charset", presets: $$(".wg-cs:checked").map((c) => c.value),
        custom: $("#wg-cs-custom").value,
        min_len: parseInt($("#wg-cs-min").value || "1", 10),
        max_len: parseInt($("#wg-cs-max").value || "1", 10),
      });
    }
    return strategies;
  }

  function wgGuard(strategies) {
    if (!strategies.length) {
      toast("Add some target words, or enable a phone/charset pattern", true);
      return false;
    }
    return true;
  }

  async function wgEstimate() {
    const el = $("#wg-result");
    const strategies = wgStrategies();
    if (!wgGuard(strategies)) return;
    try {
      const est = await API.wordgenEstimate({ strategies });
      const size = est.count ? ` · ~${fmtBytes(est.bytes_estimate)}` : "";
      const plus = est.has_unknown ? "+ word variants" : "";
      const base = est.count ? `≈ ${wgFmt(est.count)} ${plus}`.trim() : (est.has_unknown ? "word variants (deduped)" : "0");
      const warn = est.will_cap ? ` <span class="text-amber-400">— exceeds cap, will stop at ${wgFmt(est.cap)}</span>` : "";
      el.innerHTML = `<span class="text-slate-300">${base} lines${size}${warn}</span>`;
    } catch (e) { toast(e.message, true); el.textContent = ""; }
  }

  async function wgPreview() {
    const strategies = wgStrategies();
    if (!wgGuard(strategies)) return;
    const box = $("#wg-preview-box");
    try {
      const { sample } = await API.wordgenPreview({ strategies });
      box.textContent = sample.length ? sample.join("\n") + "\n…" : "(no output)";
      box.classList.remove("hidden");
    } catch (e) { toast(e.message, true); }
  }

  let wgJobId = null;
  let wgPollTimer = null;
  let wgTarget = 0;

  async function wgGenerate() {
    const strategies = wgStrategies();
    if (!wgGuard(strategies)) return;
    const payload = {
      filename: $("#wg-filename").value.trim(),
      strategies,
      target_lines: parseInt($("#wg-target").value || "0", 10) || undefined,
      lines_per_file: parseInt($("#wg-split").value || "0", 10) || 0,
      dedupe: $("#wg-dedupe").checked,
    };
    try {
      const res = await API.wordgenGenerate(payload);
      wgJobId = res.job_id;
      wgTarget = res.target || 0;
      $("#wg-result").textContent = "";
      $("#wg-preview-box").classList.add("hidden");
      $("#wg-progress").classList.remove("hidden");
      $("#wg-stop").classList.remove("hidden");
      $("#wg-generate").disabled = true;
      $("#wg-prog-file").textContent = `→ ${payload.filename || "wordlist.txt"}`;
      wgSetState("running");
      beginWgPoll();
    } catch (e) { toast(e.message, true); }
  }

  function wgSetState(state) {
    const el = $("#wg-prog-state");
    el.textContent = state;
    el.className = "crack-state " + (state === "done" ? "found"
      : ["stopped", "disk_full", "error"].includes(state) ? "notfound" : "");
  }

  function renderWgProgress(st) {
    $("#wg-prog-lines").textContent = Number(st.lines).toLocaleString();
    $("#wg-prog-bytes").textContent = fmtBytes(st.bytes);
    $("#wg-prog-rate").textContent = Number(st.rate).toLocaleString();
    $("#wg-prog-files").textContent = (st.files || []).length;
    if (wgTarget) $("#wg-prog-bar").style.width = Math.min(100, st.lines * 100 / wgTarget) + "%";
  }

  function beginWgPoll() {
    endWgPoll();
    const id = wgJobId;
    let misses = 0;
    wgPollTimer = setInterval(async () => {
      if (wgJobId !== id) return;
      try {
        const st = await API.wordgenJob(id);
        misses = 0;
        renderWgProgress(st);
        if (st.done) endWgGenerate(st);
      } catch (_) { if (++misses >= 5) endWgGenerate(null); }
    }, 700);
  }
  function endWgPoll() { if (wgPollTimer) { clearInterval(wgPollTimer); wgPollTimer = null; } }

  function endWgGenerate(st) {
    endWgPoll();
    $("#wg-stop").classList.add("hidden");
    $("#wg-generate").disabled = false;
    if (!st) { wgSetState("error"); return; }
    wgSetState(st.status);
    const filesTxt = st.files && st.files.length > 1 ? ` across ${st.files.length} files` : "";
    const dd = st.dedupe_capped ? ' <span class="text-amber-400">(dedupe hit 20M cap → continued without)</span>' : "";
    if (st.status === "done") {
      $("#wg-result").innerHTML = `<span class="text-emerald-400">✓ ${Number(st.lines).toLocaleString()} lines · ${fmtBytes(st.bytes)}${filesTxt}</span>${dd}`;
      toast(`Wordlist ready: ${Number(st.lines).toLocaleString()} lines`, "info");
    } else if (st.status === "stopped") {
      $("#wg-result").innerHTML = `<span class="text-amber-400">Stopped at ${Number(st.lines).toLocaleString()} lines · ${fmtBytes(st.bytes)} (partial kept)</span>${dd}`;
    } else if (st.status === "disk_full") {
      $("#wg-result").innerHTML = `<span class="text-amber-400">Stopped — low disk. Kept ${Number(st.lines).toLocaleString()} lines${filesTxt}.</span>`;
    } else {
      $("#wg-result").innerHTML = `<span class="text-red-400">Error: ${escapeHtml(st.error || "generation failed")}</span>`;
    }
    loadWordlists();
    loadCrackOptions();
  }

  async function wgStop() {
    if (!wgJobId) return;
    try { await API.wordgenStop(wgJobId); } catch (_) {}
    toast("Stopping generation…", "info");
  }

  function wgBuildPhoneMask() {
    const prefix = $("#wg-phone-prefix").value.trim();
    const n = Math.max(0, Math.min(12, parseInt($("#wg-phone-xcount").value || "0", 10)));
    $("#wg-phone-mask").value = prefix + "X".repeat(n);
  }

  function setupWordgen() {
    // Add-on toggles reveal their sub-options.
    $$(".wg-addon").forEach((cb) => cb.addEventListener("change", () => {
      const body = cb.closest(".wg-group").querySelector(`.wg-addon-body[data-addon="${cb.id.replace("wg-en-", "")}"]`);
      if (body) body.classList.toggle("on", cb.checked);
    }));
    $$(".wg-preset").forEach((b) => b.addEventListener("click", () => { $("#wg-target").value = b.dataset.n; }));
    $("#wg-estimate").addEventListener("click", wgEstimate);
    $("#wg-preview").addEventListener("click", wgPreview);
    $("#wg-generate").addEventListener("click", wgGenerate);
    $("#wg-stop").addEventListener("click", wgStop);
    $("#wg-phone-build").addEventListener("click", wgBuildPhoneMask);
  }

  // --- Change-password modal ----------------------------------------------
  function setupPasswordModal() {
    const modal = $("#passwd-modal");
    const open = () => { $("#pm-error").textContent = ""; $("#passwd-form").reset();
      modal.classList.remove("hidden"); $("#pm-current").focus(); };
    const close = () => modal.classList.add("hidden");
    $("#passwd-btn").addEventListener("click", open);
    $("#pm-cancel").addEventListener("click", close);
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    $("#passwd-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const cur = $("#pm-current").value, nw = $("#pm-new").value, cf = $("#pm-confirm").value;
      const err = $("#pm-error");
      if (nw.length < 4) { err.textContent = "New password must be at least 4 characters"; return; }
      if (nw !== cf) { err.textContent = "New passwords don't match"; return; }
      try {
        await API.changePassword(cur, nw);
        close();
        toast("Password updated", "info");
      } catch (ex) { err.textContent = ex.message; }
    });
  }

  // --- utils --------------------------------------------------------------
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // --- init ---------------------------------------------------------------
  function init() {
    // Build collapsible sections + the "expand/collapse all" control first.
    setupCollapsibles();

    // Nav buttons smooth-scroll to (and expand) their section.
    $("#tabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".tab-btn");
      if (btn && btn.dataset.tab) goToSection(btn.dataset.tab);
    });
    $("#iface-refresh").addEventListener("click", loadInterfaces);
    $("#scan-start").addEventListener("click", startScan);
    $("#scan-stop").addEventListener("click", stopScan);
    $("#t-capture").addEventListener("click", captureTarget);
    $("#t-reveal").addEventListener("click", revealTarget);
    $("#t-stop-capture").addEventListener("click", stopAll);
    $("#t-deauth").addEventListener("click", deauthTarget);
    $("#cap-refresh").addEventListener("click", () => { loadCaptures(); loadWordlists(); });
    $("#cap-analyze").addEventListener("click", analyzeCaptures);
    $("#cap-clean").addEventListener("click", cleanCaptures);
    $("#cap-del-selected").addEventListener("click", deleteSelectedCaptures);
    $("#cap-del-all").addEventListener("click", deleteAllCaptures);
    $("#cap-select-all").addEventListener("change", (e) => {
      $$(".cap-sel").forEach((c) => { c.checked = e.target.checked; });
    });
    $("#hs-refresh").addEventListener("click", loadHandshakes);
    $("#wl-form").addEventListener("submit", uploadWordlist);
    $("#crack-start").addEventListener("click", startCrack);
    $("#crack-stop").addEventListener("click", stopCrack);

    API.health()
      .then((h) => setHealth(h.use_sudo ? "ready (sudo)" : "ready", true))
      .catch(() => { setHealth("backend unreachable", false); toast("backend unreachable", true); });

    // Show the signed-in user + wire logout / change-password.
    API.me().then((m) => {
      if (m.authenticated) {
        $("#user-name").textContent = m.user;
        $("#pm-user").textContent = m.user;
        $("#user-pill").classList.remove("hidden");
        $("#passwd-btn").classList.remove("hidden");
        $("#logout-btn").classList.remove("hidden");
      }
    }).catch(() => {});
    $("#logout-btn").addEventListener("click", async () => {
      try { await API.logout(); } catch (_) {}
      window.location.href = "/login";
    });
    setupPasswordModal();

    // All sections are visible at once, so load every section's data up front.
    loadInterfaces();
    loadHandshakes();
    loadCaptures();
    loadWordlists();
    loadCrackOptions();
    loadRegulatory();
    setupWordgen();
    setupSectionSpy();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
