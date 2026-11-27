(function () {
  const api = () => (window.pywebview && window.pywebview.api ? window.pywebview.api : null);

  let latestState = null;
  let recordingTarget = null; // 'watch' | 'deafen' | null
  let resetArmed = false;
  let resetTimer = null;
  let pollTimer = null;
  let mutationEpoch = 0;
  let activeMutations = 0;
  let activeSlider = null;

  const $ = (id) => document.getElementById(id);

  function setSliderFill(el, value) {
    if (!el || activeSlider === el) return;
    const pct = Math.max(0, Math.min(100, Number(value) || 0));
    if (Number(el.value) !== pct) {
      el.value = pct;
    }
    el.style.setProperty("--fill", pct + "%");
  }

  let savedHideTimer = null;

  function flashSaved() {
    document.querySelectorAll(".saved").forEach((el) => el.classList.add("show"));
    clearTimeout(savedHideTimer);
    savedHideTimer = setTimeout(() => {
      document.querySelectorAll(".saved").forEach((el) => el.classList.remove("show"));
    }, 1800);
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function mutate(fn) {
    if (!api()) return null;
    mutationEpoch += 1;
    const myEpoch = mutationEpoch;
    activeMutations += 1;
    try {
      const st = await fn(api());
      if (st && myEpoch === mutationEpoch) {
        applyState(st);
      }
      return st;
    } finally {
      activeMutations = Math.max(0, activeMutations - 1);
    }
  }

  function tileMeta(procName, displayName, iconDataUrl) {
    if (iconDataUrl) {
      return {
        cls: "has-img",
        svg: `<img class="tile-img" src="${iconDataUrl}" alt="" />`,
      };
    }
    const p = (procName || "").toLowerCase();
    if (p.includes("discord")) {
      return {
        cls: "t-discord",
        svg: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.54 5.33A16.5 16.5 0 0 0 15.42 4a11.3 11.3 0 0 0-.52 1.07 15.2 15.2 0 0 0-5.8 0A11.3 11.3 0 0 0 8.58 4a16.5 16.5 0 0 0-4.12 1.33C1.86 9.35 1.15 13.27 1.5 17.13a16.6 16.6 0 0 0 5.04 2.57c.41-.56.77-1.15 1.08-1.78-.59-.22-1.16-.5-1.7-.82.14-.11.28-.22.42-.33 3.28 1.53 6.84 1.53 10.08 0 .14.11.28.22.42.33-.54.32-1.11.6-1.7.82.31.63.67 1.22 1.08 1.78a16.6 16.6 0 0 0 5.04-2.57c.41-4.47-.7-8.35-2.72-11.8ZM8.98 14.77c-.98 0-1.78-.91-1.78-2.02s.78-2.02 1.78-2.02c1 0 1.8.91 1.78 2.02 0 1.11-.78 2.02-1.78 2.02Zm6.04 0c-.98 0-1.78-.91-1.78-2.02s.78-2.02 1.78-2.02c1 0 1.8.91 1.78 2.02 0 1.11-.78 2.02-1.78 2.02Z"/></svg>',
      };
    }
    if (p.includes("chrome")) {
      return {
        cls: "t-chrome",
        svg: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="#60a5fa" stroke-width="1.8"/><circle cx="12" cy="12" r="3.5" fill="#60a5fa"/><path d="M12 8.5h8.29M15.03 13.75l-4.15 7.18M8.97 13.75L4.82 6.57" stroke="#60a5fa" stroke-width="1.7" stroke-linecap="round"/></svg>',
      };
    }
    if (p.includes("obs")) {
      return {
        cls: "t-obs",
        svg: '<svg viewBox="0 0 24 24" fill="none" stroke="#e5e5e7" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 6.5a3.5 3.5 0 0 1 3.03 5.25M7.24 14.75a3.5 3.5 0 0 1 6.06 0M16.76 14.75a3.5 3.5 0 0 1-3.03-5.25"/></svg>',
      };
    }
    if (p.includes("explorer")) {
      return {
        cls: "t-explorer",
        svg: '<svg viewBox="0 0 24 24" fill="none" stroke="#e5b93b" stroke-width="1.7" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
      };
    }
    if (p.includes("spotify")) {
      return {
        cls: "t-spotify",
        svg: '<svg viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M7.5 9.8c3.2-.9 6.6-.6 9.2.7M8.3 12.8c2.6-.7 5.2-.4 7.4.6M9.2 15.7c1.9-.5 3.8-.3 5.4.4"/></svg>',
      };
    }
    const letter = escapeHtml((displayName || procName || "?").charAt(0).toUpperCase());
    return { cls: "", svg: `<span>${letter}</span>` };
  }

  function showPage(page) {
    document.querySelectorAll("nav button[data-page]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.page === page);
    });
    document.querySelectorAll(".view").forEach((v) => {
      v.classList.toggle("on", v.id === "view-" + page);
    });
  }

  function renderTestChart(testData, threshold) {
    const svg = $("test-svg");
    const empty = $("test-empty");
    const threshLine = $("test-thresh-line");
    const threshLbl = $("test-thresh-lbl");
    const thresh = Math.max(0, Math.min(100, Number(threshold) || 0));

    if (!activeSlider) {
      threshLine.style.bottom = thresh + "%";
      threshLbl.textContent = `${Math.round(thresh)}% threshold`;
    }

    const samples = (testData && testData.samples) || [];
    const duration = Math.max(1, Number((testData && testData.duration_s) || 30));
    const lastSample = samples.length ? samples[samples.length - 1] : null;
    const sig = `${samples.length}:${duration}:${lastSample ? lastSample[0] + "," + lastSample[1] : ""}`;
    if (svg.dataset.sig === sig) return;
    svg.dataset.sig = sig;

    if (!samples.length) {
      svg.innerHTML = "";
      empty.style.display = "grid";
      return;
    }
    empty.style.display = "none";

    const W = 600;
    const H = 210;
    let html = "";
    let pts = [];
    const flush = () => {
      if (pts.length >= 2) {
        const d = pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
        html += `<polyline fill="none" stroke="#5b8be4" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="${d}" />`;
      } else if (pts.length === 1) {
        html += `<circle cx="${pts[0][0].toFixed(1)}" cy="${pts[0][1].toFixed(1)}" r="2.4" fill="#5b8be4" />`;
      }
      pts = [];
    };

    for (const pair of samples) {
      const elapsed = pair[0];
      const pct = pair[1];
      if (pct === null || pct === undefined) {
        flush();
        continue;
      }
      const x = Math.max(0, Math.min(W, (elapsed / duration) * W));
      const y = Math.max(2, Math.min(H - 2, H - (pct / 100) * H));
      pts.push([x, y]);
    }
    flush();
    svg.innerHTML = html;
  }

  function renderApps(state) {
    const cfg = state.cfg || {};
    const selected = new Set((cfg.selected_apps || []).map((s) => s.toLowerCase()));
    const query = ($("apps-search").value || "").trim().toLowerCase();
    const audioMode = cfg.audio_mode === "duck" ? "duck" : "mute";
    const pct = Math.round(Number(cfg.mute_at_percent ?? 50));

    $("apps-heading").textContent = audioMode === "duck" ? "Apps to duck" : "Apps to mute";
    if ($("apps-lead")) {
      $("apps-lead").textContent = "Geometry Dash is always excluded and will never be muted.";
    }
    const nSel = selected.size;
    $("apps-sum-count").textContent = `${nSel} ${nSel === 1 ? "app" : "apps"} selected`;
    $("apps-sum-desc").textContent = `${audioMode === "duck" ? "Ducked" : "Muted"} past ${pct}% and restored when the attempt ends.`;
    $("apps-sum-thresh").textContent = `${pct}%`;
    $("apps-sum-label").textContent = "Threshold";
    if (document.activeElement !== $("apps-bg-check")) {
      $("apps-bg-check").checked = Boolean(cfg.show_background_audio);
    }

    const targets = (state.targets || []).filter((t) => {
      if (!query) return true;
      return (
        (t.display_name || "").toLowerCase().includes(query) ||
        (t.process_name || "").toLowerCase().includes(query) ||
        (t.window_titles || []).some((w) => w.toLowerCase().includes(query))
      );
    });

    const body = $("apps-list-body");
    $("apps-count-foot").textContent = `${targets.length} ${targets.length === 1 ? "window" : "windows"} found`;

    const sig = JSON.stringify({
      q: query,
      mode: audioMode,
      sel: Array.from(selected).sort(),
      rows: targets.map((t) => [t.process_name, t.display_name, t.window_titles, t.has_audio, t.currently_muted, Boolean(t.icon_data_url)]),
    });
    if (body.dataset.sig === sig) {
      return;
    }
    body.dataset.sig = sig;

    if (!targets.length) {
      body.innerHTML = `<div class="list-empty">${
        query
          ? "No open windows match your search."
          : "No open windows detected. Open an app and click Refresh windows."
      }</div>`;
      return;
    }

    body.innerHTML = targets
      .map((t) => {
        const isSel = selected.has((t.process_name || "").toLowerCase());
        const meta = tileMeta(t.process_name, t.display_name, t.icon_data_url);
        const winTxt =
          t.window_titles && t.window_titles.length
            ? t.window_titles.slice(0, 2).join(" · ")
            : "Background audio session";

        let pillCls = "window";
        let pillTxt = "Window only";
        if (t.currently_muted) {
          pillCls = "muted";
          pillTxt = "Muted";
        } else if (t.has_audio) {
          pillCls = "active";
          pillTxt = "Audio active";
        }

        const actTxt = isSel ? (audioMode === "duck" ? "Will duck" : "Will mute") : "Select";
        return `
          <div class="trow" data-proc="${escapeHtml(t.process_name)}">
            <div class="app-cell">
              <span class="tile ${meta.cls}">${meta.svg}</span>
              <div style="min-width:0">
                <div class="app-title-line">
                  <span class="app-name">${escapeHtml(t.display_name)}</span>
                  <span class="app-exe">${escapeHtml(t.process_name)}</span>
                </div>
                <div class="app-win">${escapeHtml(winTxt)}</div>
              </div>
            </div>
            <div><span class="pill ${pillCls}"><span class="dot"></span>${pillTxt}</span></div>
            <button type="button" class="act ${isSel ? "on" : ""}">${actTxt}</button>
          </div>
        `;
      })
      .join("");
  }

  function applyRegionBoxPosition(cfg) {
    const box = $("cap-region-box");
    const lbl = $("cap-region-box-lbl");
    const mode = cfg.watch_mode;
    const reg = cfg.custom_region;

    if (mode === "custom_region" && reg) {
      if (reg.kind === "window_rel") {
        const x = Math.max(0, Math.min(85, Number(reg.rx || 0) * 100));
        const y = Math.max(0, Math.min(85, Number(reg.ry || 0) * 100));
        const w = Math.max(8, Math.min(100 - x, Number(reg.rw || 0.3) * 100));
        const h = Math.max(8, Math.min(100 - y, Number(reg.rh || 0.12) * 100));
        box.style.left = x + "%";
        box.style.right = "auto";
        box.style.width = w + "%";
        box.style.top = y + "%";
        box.style.height = h + "%";
        lbl.textContent = reg.preset || "Custom region";
      } else {
        box.style.left = "20%";
        box.style.right = "20%";
        box.style.width = "auto";
        box.style.top = "18%";
        box.style.height = "18%";
        lbl.textContent = "Custom screen";
      }
    } else {
      box.style.left = "24%";
      box.style.right = "24%";
      box.style.width = "auto";
      box.style.top = "8%";
      box.style.height = "15%";
      lbl.textContent = "Auto · Top 10%";
    }
  }

  function applyState(state) {
    if (!state) return;
    latestState = state;
    const cfg = state.cfg || {};
    const status = state.status || {};
    const reading = state.reading || {};
    const test = state.test || {};
    const obs = state.obs || {};
    const audioMode = cfg.audio_mode === "duck" ? "duck" : "mute";
    const threshold = Math.round(Number(cfg.mute_at_percent ?? 50));
    const nApps = (cfg.selected_apps || []).length;

    // Sidebar
    $("app-version").textContent = "v" + (state.app_version || "");
    $("nav-apps-label").textContent = audioMode === "duck" ? "Apps to duck" : "Apps to mute";
    $("nav-apps-badge").textContent = String(nApps);

    const wHead = $("watcher-head");
    const wantHeadCls = "watcher-head " + (status.code || "watching");
    if (wHead.className !== wantHeadCls) wHead.className = wantHeadCls;
    const rawHead = status.headline || "Watching";
    $("watcher-state-txt").textContent = rawHead.charAt(0).toUpperCase() + rawHead.slice(1).toLowerCase();
    $("watcher-pct").textContent =
      reading.percent !== null && reading.percent !== undefined ? `${reading.percent.toFixed(1)} %` : "-- %";
    $("watcher-sub").textContent = status.sub || "Waiting for a run";

    $("watcher-thresh").textContent = `${audioMode === "duck" ? "Ducks at" : "Mutes past"} ${threshold}%`;

    $("sidebar-watch-txt").textContent = cfg.watch_enabled ? "Pause watching" : "Resume watching";
    const whk = cfg.watch_toggle_hotkey;
    const hkKbd = $("sidebar-hk-kbd");
    if (whk && whk.label) {
      hkKbd.textContent = whk.label;
      hkKbd.style.display = "inline-block";
    } else {
      hkKbd.style.display = "none";
    }
    $("sidebar-restore-btn").textContent =
      audioMode === "duck" ? "Restore audio & undeafen" : "Unmute & undeafen all";

    const isBusy = Boolean(state.updating || state.checking_updates);
    const updBtn = $("sidebar-update-btn");
    const setUpdBtn = $("settings-check-updates-btn");
    const bannerInstallBtn = $("update-banner-install");
    updBtn.disabled = isBusy;
    if (setUpdBtn) setUpdBtn.disabled = isBusy;
    if (bannerInstallBtn) {
      bannerInstallBtn.disabled = Boolean(state.updating);
      bannerInstallBtn.textContent = state.updating ? state.update_btn_text || "Updating..." : "Update now";
    }

    const downBanner = $("downgrade-banner");
    const downBannerBtn = $("downgrade-banner-btn");
    const setDownBtn = $("settings-downgrade-btn");
    const betaNote = $("settings-beta-note");
    if (state.is_beta_build) {
      const stableVer = state.latest_stable && state.latest_stable.version;
      if (setDownBtn) {
        setDownBtn.style.display = "inline-flex";
        setDownBtn.disabled = isBusy;
        setDownBtn.textContent = stableVer ? `Downgrade to v${stableVer}` : "Downgrade to latest stable";
      }
      if (betaNote) {
        betaNote.style.display = "block";
        betaNote.textContent = stableVer
          ? `Running beta v${state.app_version}. You can downgrade to stable v${stableVer} anytime.`
          : `Running beta v${state.app_version}. You can downgrade to the latest stable release anytime.`;
      }
      if (downBanner && downBannerBtn) {
        if (state.show_downgrade_banner && state.latest_stable) {
          $("downgrade-banner-txt").textContent = `You opted out of beta updates (currently on v${state.app_version}). Downgrade to latest stable v${state.latest_stable.version}?`;
          downBannerBtn.disabled = Boolean(state.updating);
          downBannerBtn.textContent = state.updating ? state.update_btn_text || "Downgrading..." : `Downgrade to v${state.latest_stable.version}`;
          downBanner.classList.add("show");
        } else {
          downBanner.classList.remove("show");
        }
      }
    } else {
      if (setDownBtn) setDownBtn.style.display = "none";
      if (betaNote) betaNote.style.display = "none";
      if (downBanner) downBanner.classList.remove("show");
    }

    if (state.updating || state.checking_updates) {
      $("sidebar-update-txt").textContent = state.update_btn_text || (state.updating ? "Updating..." : "Checking...");
      updBtn.classList.remove("has-update");
      if (setUpdBtn) setUpdBtn.textContent = state.update_btn_text || "Checking...";
    } else if (state.pending_update && state.pending_update.latest_version) {
      $("sidebar-update-txt").textContent = `Install v${state.pending_update.latest_version} update`;
      updBtn.classList.add("has-update");
      if (setUpdBtn) {
        setUpdBtn.textContent = `Install v${state.pending_update.latest_version} update`;
      }
    } else {
      $("sidebar-update-txt").textContent = state.update_btn_text || "Check for updates";
      updBtn.classList.remove("has-update");
      if (setUpdBtn) {
        setUpdBtn.textContent = "Check for updates now";
      }
    }
    $("footer-msg").textContent = state.footer_message || "";

    // Update banner
    const uBanner = $("update-banner");
    if (state.show_update_banner && state.pending_update) {
      $("update-banner-txt").textContent = `New version v${state.pending_update.latest_version} is available (current: v${state.app_version}).`;
      uBanner.classList.add("show");
    } else {
      uBanner.classList.remove("show");
    }

    // Level detection prompt
    const lBanner = $("level-banner");
    if (state.level_prompt) {
      $("level-banner-txt").textContent = `Detected level "${state.level_prompt.display}". Save profile for this level?`;
      lBanner.classList.add("show");
    } else {
      lBanner.classList.remove("show");
      $("level-corr-row").classList.remove("show");
    }

    // Overview
    $("ov-threshold-title").textContent = audioMode === "duck" ? "Duck threshold" : "Mute threshold";
    $("ov-threshold-desc").textContent =
      audioMode === "duck"
        ? "Lower selected app volume when your progress passes the threshold."
        : "Mute selected apps when your progress passes the threshold.";
    if (document.activeElement !== $("ov-pct-input") && !activeSlider) {
      $("ov-pct-input").value = String(threshold);
    }
    setSliderFill($("ov-threshold"), threshold);
    $("ov-unmute100-tog").classList.toggle("on", Boolean(cfg.unmute_at_100));

    if (recordingTarget !== "watch") {
      $("ov-hk-kbd").textContent = whk && whk.label ? whk.label : "Not set";
    }
    const hkStatusEl = $("ov-hk-status");
    hkStatusEl.textContent = state.watch_hotkey_status || "Inactive";
    hkStatusEl.classList.toggle("ok", (state.watch_hotkey_status || "").toLowerCase().includes("active"));

    $("ov-setup-apps").textContent = `${nApps} selected`;
    const dhk = cfg.deafen_hotkey;
    const deafenSetup = $("ov-setup-deafen");
    deafenSetup.classList.remove("ok", "warn");
    if (cfg.auto_deafen && dhk && dhk.label) {
      deafenSetup.textContent = `${dhk.label} · Ready`;
      deafenSetup.classList.add("ok");
    } else if (cfg.auto_deafen) {
      deafenSetup.textContent = "Set hotkey";
      deafenSetup.classList.add("warn");
    } else {
      deafenSetup.textContent = "Off";
    }
    $("ov-setup-capture").textContent = `${state.capture_short || "Auto"} · ${cfg.target_process || "geometrydash.exe"}`;
    $("ov-level-profile").textContent = state.active_level_profile || "No confirmed level profile";
    $("ov-level-status").textContent = state.level_detection_status || "";

    // 30s test
    $("test-toggle-btn").textContent = test.active ? "Stop test" : "Start 30s test";
    $("test-progress-fill").style.width = `${Math.max(0, Math.min(100, (test.ratio || 0) * 100))}%`;
    $("test-status").textContent = test.status_text || "Ready to test";
    $("test-live").textContent =
      test.last_percent !== null && test.last_percent !== undefined ? `${test.last_percent.toFixed(2)}%` : "--%";
    $("test-peak").textContent =
      test.peak_percent !== null && test.peak_percent !== undefined ? `${test.peak_percent.toFixed(2)}%` : "--%";
    $("test-reads").textContent = String(test.valid_reads || 0);
    $("test-duration").textContent = `${Math.round(Number(cfg.test_duration_s ?? 30))} seconds`;
    renderTestChart(test, threshold);

    const milestones = test.milestones || [];
    const seenList = $("test-seen-list");
    const mSig = milestones.length ? `${milestones.length}:${milestones[milestones.length - 1]}` : "0";
    if (seenList.dataset.sig !== mSig) {
      seenList.dataset.sig = mSig;
      if (milestones.length) {
        $("test-seen-empty").style.display = "none";
        seenList.innerHTML = milestones.map((m) => `<li>${escapeHtml(m)}</li>`).join("");
        seenList.scrollTop = seenList.scrollHeight;
      } else {
        $("test-seen-empty").style.display = "block";
        seenList.innerHTML = "";
      }
    }

    const note = $("test-note");
    note.classList.remove("pass", "warn");
    if (test.verdict) {
      $("test-note-txt").textContent = test.verdict;
      if (test.completed) {
        note.classList.add(test.passed ? "pass" : "warn");
      }
    }

    // Apps
    renderApps(state);

    // Discord auto-deafen
    $("deafen-desc").textContent = `Automatically toggle Discord deafen past ${threshold}%, then undeafen on death, 100%, or exit.`;
    $("deafen-toggle").classList.toggle("on", Boolean(cfg.auto_deafen));
    if (recordingTarget !== "deafen") {
      $("deafen-hk-input").value = dhk && dhk.label ? dhk.label : "Not set";
    }

    const dPill = $("deafen-status-pill");
    let dCls = "ready";
    let dTxt = "Disabled";
    if (status.deafened_by_us) {
      dCls = "ready bad";
      dTxt = "Deafened by GD Quiet";
    } else if (cfg.auto_deafen && dhk && dhk.label) {
      dCls = "ready ok";
      dTxt = "Auto-undeafen ready";
    } else if (cfg.auto_deafen) {
      dCls = "ready warn";
      dTxt = "Set hotkey";
    }
    if (dPill.className !== dCls) dPill.className = dCls;
    $("deafen-status-txt").textContent = dTxt;

    const step3 = $("deafen-step3-badge");
    if (cfg.auto_deafen && dhk && dhk.label) {
      step3.className = "step-badge ok";
      step3.textContent = `Ready (${dhk.label})`;
    } else if (dhk && dhk.label) {
      step3.className = "step-badge warn";
      step3.textContent = "Enable toggle above";
    } else {
      step3.className = "step-badge warn";
      step3.textContent = "Needs hotkey";
    }

    // Screen capture
    const sel = $("cap-target-select");
    const rawProcs = state.open_processes || [];
    const procItems = [];
    const seenProcs = new Set();
    for (const item of rawProcs) {
      const val = typeof item === "string" ? item : item && item.value;
      const lbl = typeof item === "string" ? item : (item && item.label) || val;
      if (val && !seenProcs.has(val)) {
        seenProcs.add(val);
        procItems.push({ value: val, label: lbl });
      }
    }
    const wantTarget = cfg.target_process || "geometrydash.exe";
    if (wantTarget && !seenProcs.has(wantTarget)) {
      procItems.unshift({ value: wantTarget, label: wantTarget });
    }
    const procsKey = JSON.stringify(procItems);
    if (sel.dataset.procsKey !== procsKey) {
      sel.dataset.procsKey = procsKey;
      sel.innerHTML = procItems
        .map((p) => `<option value="${escapeHtml(p.value)}">${escapeHtml(p.label)}</option>`)
        .join("");
    }
    if (document.activeElement !== sel && sel.value !== wantTarget) {
      sel.value = wantTarget;
    }

    const wBanner = $("cap-watch-banner");
    const watchingOn = Boolean(cfg.watch_enabled);
    wBanner.classList.toggle("ok", watchingOn);
    $("cap-watch-banner-txt").textContent = watchingOn
      ? reading.status_text || "Watching screen..."
      : "Screen watcher paused · No screen data is being read.";

    applyRegionBoxPosition(cfg);

    const prevImg = $("cap-preview-img");
    const prevPh = $("cap-preview-placeholder");
    const regionBox = $("cap-region-box");
    if (reading.preview_data_url && watchingOn) {
      if (prevImg.dataset.lastSrc !== reading.preview_data_url) {
        prevImg.dataset.lastSrc = reading.preview_data_url;
        prevImg.src = reading.preview_data_url;
      }
      prevImg.classList.add("show");
      prevPh.classList.add("hide");
      regionBox.classList.add("hide");
    } else {
      prevImg.classList.remove("show");
      prevPh.classList.remove("hide");
      regionBox.classList.remove("hide");
      $("cap-ph-title").textContent = watchingOn ? "Waiting for capture" : "Capture is paused";
      $("cap-ph-sub").textContent = watchingOn
        ? "Open Geometry Dash to see live screen data."
        : "Resume watching to see live screen data.";
    }

    $("cap-detected-pct").textContent =
      reading.percent !== null && reading.percent !== undefined ? `${reading.percent.toFixed(2)} %` : "-- %";
    $("cap-status-txt").textContent = reading.status_text || (watchingOn ? "Waiting for Geometry Dash..." : "Screen watcher paused");
    $("cap-ocr-diag").textContent = reading.diagnostic || "Raw OCR: (waiting)";
    const pillCls = "pill " + (watchingOn ? "active" : "paused");
    if ($("cap-pill").className !== pillCls) $("cap-pill").className = pillCls;
    $("cap-pill-txt").textContent = watchingOn ? status.headline || "Watching" : "Paused";

    const activePreset =
      cfg.watch_mode === "custom_region" && cfg.custom_region && cfg.custom_region.preset
        ? cfg.custom_region.preset
        : cfg.watch_mode === "auto_window"
        ? "auto"
        : "";
    document.querySelectorAll("#cap-opts .opt").forEach((b) => {
      b.classList.toggle("on", b.dataset.preset === activePreset);
    });
    $("cap-region-desc").textContent = state.region_description || "Auto top 10%";

    // Settings
    document.querySelectorAll("#view-settings input[data-num-key]").forEach((inp) => {
      if (document.activeElement === inp) return;
      const key = inp.dataset.numKey;
      const val = cfg[key];
      if (val !== undefined && val !== null && inp.value !== String(val)) {
        inp.value = String(val);
      }
    });
    document.querySelectorAll("#view-settings button.toggle[data-bool-key]").forEach((tog) => {
      const key = tog.dataset.boolKey;
      tog.classList.toggle("on", Boolean(cfg[key]));
    });
    if (document.activeElement !== $("set-audio-mode") && $("set-audio-mode").value !== audioMode) {
      $("set-audio-mode").value = audioMode;
    }
    const fadeMode = cfg.fade_in_mode || "off";
    if (document.activeElement !== $("set-fade-mode") && $("set-fade-mode").value !== fadeMode) {
      $("set-fade-mode").value = fadeMode;
    }

    const obsBadge = $("obs-status-badge");
    obsBadge.classList.toggle("off", !obs.running);
    $("obs-status-txt").textContent = obs.status_text || (obs.running ? "Running locally" : "Stopped");
    if ($("obs-url-input").value !== (obs.url || "")) {
      $("obs-url-input").value = obs.url || "";
    }
    $("obs-toggle-btn").textContent = obs.running ? "Stop OBS overlay" : "Enable OBS overlay";
    $("config-path-box").textContent = state.config_path || "";
  }

  function jsKeyEventToSpec(e) {
    const modKeys = new Set(["Control", "Shift", "Alt", "Meta"]);
    if (modKeys.has(e.key)) return null;

    const mods = [];
    if (e.ctrlKey) mods.push("ctrl");
    if (e.shiftKey) mods.push("shift");
    if (e.altKey) mods.push("alt");

    const vk = e.keyCode || 0;
    if (!vk) return null;

    let keyLabel = e.key;
    if (keyLabel === " ") keyLabel = "Space";
    else if (keyLabel.length === 1) keyLabel = keyLabel.toUpperCase();
    else if (/^F\d{1,2}$/i.test(keyLabel)) keyLabel = keyLabel.toUpperCase();

    const parts = [];
    if (mods.includes("ctrl")) parts.push("Ctrl");
    if (mods.includes("shift")) parts.push("Shift");
    if (mods.includes("alt")) parts.push("Alt");
    parts.push(keyLabel);

    return { modifiers: mods, vk: vk, label: parts.join(" + ") };
  }

  function startRecording(target) {
    recordingTarget = target;
    if (document.activeElement && typeof document.activeElement.blur === "function") {
      document.activeElement.blur();
    }
    if (target === "watch") {
      $("ov-record-hk").textContent = "Press keys...";
      $("ov-hk-kbd").textContent = "Listening...";
    } else if (target === "deafen") {
      $("deafen-record-btn").textContent = "Press keys...";
      $("deafen-hk-input").value = "Listening...";
    }
  }

  function stopRecording(restore) {
    recordingTarget = null;
    $("ov-record-hk").textContent = "Record";
    $("deafen-record-btn").textContent = "Record";
    if (restore && latestState) applyState(latestState);
  }

  function bindEvents() {
    // Titlebar controls
    $("win-min").addEventListener("click", () => api() && api().window_minimize());
    $("win-max").addEventListener("click", () => api() && api().window_toggle_maximize());
    $("win-close").addEventListener("click", () => api() && api().window_close());

    // Sidebar nav & Overview quick links
    document.querySelectorAll("nav button[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => showPage(btn.dataset.page));
    });
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.addEventListener("click", () => showPage(btn.dataset.nav));
    });

    // Slider drag tracking so polls never snap the thumb back while dragging
    const releaseSlider = () => {
      activeSlider = null;
    };
    window.addEventListener("pointerup", releaseSlider);
    window.addEventListener("mouseup", releaseSlider);
    window.addEventListener("touchend", releaseSlider);

    [$("ov-threshold")].forEach((slider) => {
      if (!slider) return;
      const startDrag = () => {
        activeSlider = slider;
      };
      slider.addEventListener("pointerdown", startDrag);
      slider.addEventListener("mousedown", startDrag);
      slider.addEventListener("touchstart", startDrag);
    });

    // Sidebar controls
    $("sidebar-watch-btn").addEventListener("click", async () => {
      await mutate((a) => a.toggle_watching());
      flashSaved();
    });
    $("sidebar-restore-btn").addEventListener("click", async () => {
      await mutate((a) => a.force_restore_all());
    });
    $("sidebar-update-btn").addEventListener("click", async () => {
      await mutate((a) => a.check_or_install_update());
    });

    // Banners
    $("update-banner-install").addEventListener("click", async () => {
      await mutate((a) => a.install_pending_update());
    });
    $("update-banner-later").addEventListener("click", async () => {
      await mutate((a) => a.dismiss_update_banner());
    });
    $("downgrade-banner-btn").addEventListener("click", async () => {
      await mutate((a) => a.downgrade_to_stable());
    });
    $("downgrade-banner-later").addEventListener("click", async () => {
      await mutate((a) => a.dismiss_downgrade_banner());
    });
    $("level-yes").addEventListener("click", async () => {
      await mutate((a) => a.confirm_level_candidate());
    });
    $("level-correct").addEventListener("click", () => {
      $("level-corr-row").classList.add("show");
      $("level-corr-input").focus();
    });
    $("level-later").addEventListener("click", async () => {
      await mutate((a) => a.dismiss_level_candidate());
    });
    $("level-corr-save").addEventListener("click", async () => {
      await mutate((a) => a.save_level_correction($("level-corr-input").value));
      flashSaved();
    });

    // Overview threshold & controls
    $("ov-threshold").addEventListener("input", (e) => {
      const val = e.target.value;
      e.target.style.setProperty("--fill", val + "%");
      $("ov-pct-input").value = val;
      $("watcher-thresh").textContent = `${
        latestState && latestState.cfg && latestState.cfg.audio_mode === "duck" ? "Ducks at" : "Mutes past"
      } ${val}%`;
      $("test-thresh-line").style.bottom = val + "%";
      $("test-thresh-lbl").textContent = `${val}% threshold`;
    });
    $("ov-threshold").addEventListener("change", async (e) => {
      activeSlider = null;
      await mutate((a) => a.set_threshold(Number(e.target.value)));
      flashSaved();
    });
    const commitOvPct = async () => {
      const val = parseFloat($("ov-pct-input").value);
      if (Number.isNaN(val)) return;
      await mutate((a) => a.set_threshold(val));
      flashSaved();
    };
    $("ov-apply-btn").addEventListener("click", commitOvPct);
    $("ov-pct-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") commitOvPct();
    });

    $("ov-unmute100-tog").addEventListener("click", async () => {
      const next = !$("ov-unmute100-tog").classList.contains("on");
      $("ov-unmute100-tog").classList.toggle("on", next);
      await mutate((a) => a.set_setting("unmute_at_100", next));
      flashSaved();
    });
    $("ov-record-hk").addEventListener("click", () => {
      if (recordingTarget === "watch") stopRecording(true);
      else startRecording("watch");
    });
    $("ov-clear-hk").addEventListener("click", async () => {
      stopRecording(false);
      await mutate((a) => a.clear_watch_hotkey());
      flashSaved();
    });
    $("ov-save-setup").addEventListener("click", async () => {
      await mutate((a) => a.save_setup());
      flashSaved();
    });
    $("ov-pick-spot").addEventListener("click", async () => {
      await mutate((a) => a.pick_region_now());
    });
    $("test-toggle-btn").addEventListener("click", async () => {
      await mutate((a) => a.toggle_screen_test());
    });

    // Apps (delegated click listener so rows never drop clicks!)
    $("apps-list-body").addEventListener("click", async (e) => {
      const row = e.target.closest(".trow");
      if (!row) return;
      const proc = row.dataset.proc;
      if (!proc) return;
      const actBtn = row.querySelector(".act");
      if (actBtn) {
        const nextOn = !actBtn.classList.contains("on");
        actBtn.classList.toggle("on", nextOn);
        const mode = latestState && latestState.cfg && latestState.cfg.audio_mode === "duck" ? "duck" : "mute";
        actBtn.textContent = nextOn ? (mode === "duck" ? "Will duck" : "Will mute") : "Select";
      }
      await mutate((a) => a.toggle_app(proc));
      flashSaved();
    });

    $("apps-search").addEventListener("input", () => latestState && renderApps(latestState));
    $("apps-bg-check").addEventListener("change", async (e) => {
      const checked = e.target.checked;
      await mutate((a) => a.set_setting("show_background_audio", checked));
      flashSaved();
    });
    $("apps-refresh-btn").addEventListener("click", async () => {
      await mutate((a) => a.refresh_windows());
    });
    $("apps-clear-btn").addEventListener("click", async () => {
      await mutate((a) => a.clear_selected_apps());
      flashSaved();
    });

    // Discord auto-deafen
    $("deafen-toggle").addEventListener("click", async () => {
      const next = !$("deafen-toggle").classList.contains("on");
      $("deafen-toggle").classList.toggle("on", next);
      await mutate((a) => a.set_setting("auto_deafen", next));
      flashSaved();
    });
    $("deafen-record-btn").addEventListener("click", () => {
      if (recordingTarget === "deafen") stopRecording(true);
      else startRecording("deafen");
    });
    $("deafen-test-btn").addEventListener("click", async () => {
      await mutate((a) => a.test_deafen_hotkey());
    });
    $("deafen-clear-btn").addEventListener("click", async () => {
      stopRecording(false);
      await mutate((a) => a.clear_deafen_hotkey());
      flashSaved();
    });

    const showGuideTab = (tab) => {
      const isFixes = tab === "fixes";
      $("guide-tab-steps").classList.toggle("on", !isFixes);
      $("guide-tab-fixes").classList.toggle("on", isFixes);
      $("guide-steps").classList.toggle("off", isFixes);
      $("guide-fixes").classList.toggle("on", isFixes);
    };
    $("guide-tab-steps").addEventListener("click", () => showGuideTab("steps"));
    $("guide-tab-fixes").addEventListener("click", () => showGuideTab("fixes"));
    $("deafen-trouble-btn").addEventListener("click", () => showGuideTab("fixes"));

    // Screen capture
    $("cap-target-select").addEventListener("change", async (e) => {
      const val = e.target.value;
      await mutate((a) => a.set_target_process(val));
      flashSaved();
    });
    $("cap-refresh-btn").addEventListener("click", async () => {
      await mutate((a) => a.refresh_target_windows());
    });
    document.querySelectorAll("#cap-opts .opt").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll("#cap-opts .opt").forEach((o) => o.classList.remove("on"));
        btn.classList.add("on");
        await mutate((a) => a.set_region_preset(btn.dataset.preset));
        flashSaved();
      });
    });
    $("cap-drag-btn").addEventListener("click", async () => {
      await mutate((a) => a.pick_region_now());
    });
    $("cap-delay-btn").addEventListener("click", async () => {
      const btn = $("cap-delay-btn");
      btn.disabled = true;
      for (let s = 3; s >= 1; s--) {
        btn.textContent = `Picking in ${s}s...`;
        await new Promise((r) => setTimeout(r, 1000));
      }
      btn.textContent = "Pick with 3s delay";
      btn.disabled = false;
      await mutate((a) => a.pick_region_now());
    });

    // Settings
    document.querySelectorAll("#view-settings button.apply[data-apply-key]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const key = btn.dataset.applyKey;
        const inp = document.querySelector(`#view-settings input[data-num-key="${key}"]`);
        if (!inp) return;
        const val = parseFloat(inp.value);
        if (Number.isNaN(val)) return;
        await mutate((a) => a.set_setting(key, val));
        flashSaved();
      });
    });
    document.querySelectorAll("#view-settings input[data-num-key]").forEach((inp) => {
      inp.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter") return;
        const val = parseFloat(inp.value);
        if (Number.isNaN(val)) return;
        await mutate((a) => a.set_setting(inp.dataset.numKey, val));
        flashSaved();
      });
    });
    document.querySelectorAll("#view-settings button.toggle[data-bool-key]").forEach((tog) => {
      tog.addEventListener("click", async () => {
        const key = tog.dataset.boolKey;
        const next = !tog.classList.contains("on");
        tog.classList.toggle("on", next);
        await mutate((a) => a.set_setting(key, next));
        flashSaved();
      });
    });
    $("set-apply-audio-mode").addEventListener("click", async () => {
      await mutate((a) => a.set_setting("audio_mode", $("set-audio-mode").value));
      flashSaved();
    });
    $("set-audio-mode").addEventListener("change", async (e) => {
      await mutate((a) => a.set_setting("audio_mode", e.target.value));
      flashSaved();
    });
    $("set-apply-fade-mode").addEventListener("click", async () => {
      await mutate((a) => a.set_setting("fade_in_mode", $("set-fade-mode").value));
      flashSaved();
    });
    $("set-fade-mode").addEventListener("change", async (e) => {
      await mutate((a) => a.set_setting("fade_in_mode", e.target.value));
      flashSaved();
    });

    $("obs-copy-btn").addEventListener("click", async () => {
      const inp = $("obs-url-input");
      if (navigator.clipboard && inp.value) {
        try {
          await navigator.clipboard.writeText(inp.value);
        } catch (_) {}
      }
      await mutate((a) => a.copy_obs_url());
      $("obs-copy-btn").textContent = "Copied";
      setTimeout(() => ($("obs-copy-btn").textContent = "Copy URL"), 1400);
    });
    $("obs-toggle-btn").addEventListener("click", async () => {
      await mutate((a) => a.toggle_obs_overlay());
      flashSaved();
    });
    $("obs-preview-btn").addEventListener("click", () => {
      api() && api().preview_obs_overlay();
    });
    $("settings-check-updates-btn").addEventListener("click", async () => {
      await mutate((a) => a.check_or_install_update());
    });
    $("settings-downgrade-btn").addEventListener("click", async () => {
      await mutate((a) => a.downgrade_to_stable());
    });
    $("open-config-btn").addEventListener("click", () => {
      api() && api().open_config_folder();
    });
    $("reset-defaults-btn").addEventListener("click", async () => {
      const btn = $("reset-defaults-btn");
      if (!resetArmed) {
        resetArmed = true;
        btn.textContent = "Confirm reset?";
        clearTimeout(resetTimer);
        resetTimer = setTimeout(() => {
          resetArmed = false;
          btn.textContent = "Reset to defaults";
        }, 4000);
        return;
      }
      resetArmed = false;
      clearTimeout(resetTimer);
      btn.textContent = "Reset to defaults";
      await mutate((a) => a.reset_defaults());
      flashSaved();
    });

    // Global hotkey capture listener (capture phase so focused buttons or F5 never interfere)
    window.addEventListener(
      "keydown",
      async (e) => {
        if (recordingTarget) {
          e.preventDefault();
          e.stopPropagation();
          if (e.key === "Escape") {
            stopRecording(true);
            return;
          }
          const spec = jsKeyEventToSpec(e);
          if (!spec) return;
          const target = recordingTarget;
          stopRecording(false);
          if (target === "watch") {
            await mutate((a) => a.set_watch_hotkey(spec));
          } else {
            await mutate((a) => a.set_deafen_hotkey(spec));
          }
          flashSaved();
          return;
        }
        if (e.key === "F5" || (e.ctrlKey && (e.key === "r" || e.key === "R"))) {
          e.preventDefault();
        }
      },
      true
    );
  }

  async function pollLoop() {
    try {
      if (api() && activeMutations === 0) {
        const epochAtStart = mutationEpoch;
        const st = await api().get_state();
        if (activeMutations === 0 && epochAtStart === mutationEpoch) {
          applyState(st);
        }
      }
    } catch (_) {}
    pollTimer = setTimeout(pollLoop, 220);
  }

  window.addEventListener("pywebviewready", async () => {
    bindEvents();
    try {
      if (api()) {
        applyState(await api().get_state());
      }
    } catch (_) {}
    pollLoop();
  });
})();
