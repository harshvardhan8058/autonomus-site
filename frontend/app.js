"use strict";

/**
 * Mission-control frontend controller (Req 11, 12).
 *
 * Vanilla JS, no build step. On submit the console POSTs the request to
 * `/agent`; because that endpoint is synchronous and returns the full
 * `AgentResponse` only after the run finishes, the console then opens an
 * `EventSource` on `/agent/{run_id}/stream`, which REPLAYS the run's buffered
 * events. The animated timeline and monospace reasoning log are populated
 * purely from SSE events; the final result card is rendered from the
 * `run_completed` event (falling back to the POST response). The code is robust
 * to the replay case where every event arrives back-to-back immediately.
 *
 * This controller also drives the HUD enhancements: a live `/health` backend
 * status pill, a boot-up typewriter reveal, a live character counter and
 * Ctrl/Cmd+Enter submit, a run-stats strip (elapsed timer, progress bar,
 * counters, phase, copyable run id), timeline rail fill + per-step durations,
 * color-coded / copyable reasoning log, an enhanced result card (glow burst,
 * copy summary, open-in-new-tab, new-run reset), and a transient toast system.
 * Every animation degrades gracefully under prefers-reduced-motion.
 */

(function () {
  // ----- DOM references ---------------------------------------------------
  const form = document.getElementById("request-form");
  const input = document.getElementById("request-input");
  const submitButton = document.getElementById("submit-button");
  const chipContainer = document.getElementById("example-chips");
  const errorBanner = document.getElementById("error-banner");

  const timeline = document.getElementById("timeline");
  const timelineList = document.getElementById("timeline-list");
  const timelineRailFill = document.getElementById("timeline-rail-fill");

  const assumptionsPanel = document.getElementById("assumptions-panel");
  const assumptionsList = document.getElementById("assumptions-list");

  const reasoningLog = document.getElementById("reasoning-log");
  const reasoningOutput = document.getElementById("reasoning-output");
  const copyLogButton = document.getElementById("copy-log");

  const resultCard = document.getElementById("result-card");
  const resultStatus = document.getElementById("result-status");
  const resultSummary = document.getElementById("result-summary");
  const resultSteps = document.getElementById("result-steps");
  const downloadButton = document.getElementById("download-button");
  const openTabButton = document.getElementById("open-tab-button");
  const copySummaryButton = document.getElementById("copy-summary");
  const newRunButton = document.getElementById("new-run-button");

  // Backend status pill
  const backendStatus = document.getElementById("backend-status");
  const backendStatusText = document.getElementById("backend-status-text");

  // Boot reveal
  const bootPill = document.getElementById("boot-pill");

  // Input meta
  const charCounter = document.getElementById("char-counter");
  const CHAR_CAP = 2000;

  // Run stats HUD
  const runStats = document.getElementById("run-stats");
  const statPhase = document.getElementById("stat-phase");
  const statElapsed = document.getElementById("stat-elapsed");
  const statDone = document.getElementById("stat-done");
  const statFailed = document.getElementById("stat-failed");
  const statTotal = document.getElementById("stat-total");
  const statRunId = document.getElementById("stat-run-id");
  const copyRunIdButton = document.getElementById("copy-run-id");
  const runProgress = document.getElementById("run-progress");
  const progressFill = document.getElementById("progress-fill");
  const progressLabel = document.getElementById("progress-label");

  // Toasts
  const toastContainer = document.getElementById("toast-container");

  const reduceMotion =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ----- Run-scoped state -------------------------------------------------
  // Maps a 1-based step number to its rendered <li> row element.
  let stepRows = new Map();
  let eventSource = null;
  let runFinished = false;

  // Stats/timer state
  let totalSteps = 0;
  let doneCount = 0;
  let failedCount = 0;
  let elapsedTimer = null;
  let runStartMs = 0;
  let currentRunId = "";
  // Per-step start timestamps (ms) for client-side duration display.
  let stepStartTimes = new Map();

  // ----- Helpers ----------------------------------------------------------

  /** Append a color-coded monospace line to the reasoning log. */
  function log(line, kind) {
    const stamp = new Date().toLocaleTimeString();
    const span = document.createElement("span");
    span.className = "log-line log-" + (kind || "muted");
    span.textContent = "[" + stamp + "] " + line + "\n";
    reasoningOutput.appendChild(span);
    reasoningOutput.scrollTop = reasoningOutput.scrollHeight;
  }

  /** Show a clear error message in the UI and re-enable the form. */
  function showError(message) {
    errorBanner.textContent = message;
    errorBanner.hidden = false;
    setFormDisabled(false);
  }

  /** Clear any previously shown error message. */
  function clearError() {
    errorBanner.textContent = "";
    errorBanner.hidden = true;
  }

  /** Enable/disable the input and submit button during a run. */
  function setFormDisabled(disabled) {
    input.disabled = disabled;
    submitButton.disabled = disabled;
  }

  /** Copy text to the clipboard, flashing a "copied" confirmation on a button. */
  function copyToClipboard(text, buttonEl, confirmLabel) {
    if (!text) {
      return;
    }
    const done = function () {
      if (!buttonEl) {
        return;
      }
      const original = buttonEl.dataset.label || buttonEl.textContent;
      buttonEl.dataset.label = original;
      buttonEl.textContent = confirmLabel || "COPIED";
      buttonEl.classList.add("copied");
      window.setTimeout(function () {
        buttonEl.textContent = buttonEl.dataset.label;
        buttonEl.classList.remove("copied");
      }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    }
  }

  // ----- Toasts -----------------------------------------------------------

  /** Show a transient, auto-dismissing toast (top-right). */
  function showToast(title, message, variant) {
    if (!toastContainer) {
      return;
    }
    const toast = document.createElement("div");
    toast.className = "toast toast-" + (variant || "info");
    toast.setAttribute("role", "status");

    const titleEl = document.createElement("span");
    titleEl.className = "toast-title";
    titleEl.textContent = title;
    toast.appendChild(titleEl);

    if (message) {
      const body = document.createElement("span");
      body.className = "toast-body";
      body.textContent = message;
      toast.appendChild(body);
    }

    toastContainer.appendChild(toast);

    const remove = function () {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    };

    window.setTimeout(function () {
      if (reduceMotion) {
        remove();
      } else {
        toast.classList.add("toast-out");
        window.setTimeout(remove, 300);
      }
    }, 4000);
  }

  // ----- Backend status pill (Feature 1) ---------------------------------

  /** Poll `/health` and update the backend status pill; fail silently. */
  async function refreshBackendStatus() {
    if (!backendStatus || !backendStatusText) {
      return;
    }
    try {
      const resp = await fetch("/health", { headers: { Accept: "application/json" } });
      const data = await resp.json();
      const backend = (data.llm_backend || "unknown").toUpperCase();
      if (data.backend_ready) {
        backendStatus.classList.remove("is-offline");
        backendStatus.classList.add("is-online");
        backendStatusText.textContent = backend + " \u2022 ONLINE";
      } else {
        backendStatus.classList.remove("is-online");
        backendStatus.classList.add("is-offline");
        backendStatusText.textContent = backend + " \u2022 OFFLINE";
      }
    } catch (err) {
      backendStatus.classList.remove("is-online");
      backendStatus.classList.add("is-offline");
      backendStatusText.textContent = "\u2022 OFFLINE";
    }
  }

  // ----- Boot reveal (Feature 2) -----------------------------------------

  /** Type text into an element character-by-character (reduced-motion: instant). */
  function typewriter(el, text, speed, done) {
    if (!el) {
      if (done) done();
      return;
    }
    if (reduceMotion) {
      el.textContent = text;
      if (done) done();
      return;
    }
    el.textContent = "";
    el.classList.add("typing");
    let i = 0;
    const tick = function () {
      if (i <= text.length) {
        el.textContent = text.slice(0, i);
        i += 1;
        window.setTimeout(tick, speed);
      } else {
        el.classList.remove("typing");
        if (done) done();
      }
    };
    tick();
  }

  /** Run the boot-up reveal: typewriter title + description, blinking pill. */
  function bootReveal() {
    const titleEl = document.getElementById("app-title");
    const descEl = document.getElementById("app-description");
    if (bootPill && !reduceMotion) {
      bootPill.classList.add("booting");
      window.setTimeout(function () {
        bootPill.classList.remove("booting");
      }, 2800);
    }
    if (reduceMotion) {
      // Text already present in the static DOM; nothing to animate.
      return;
    }
    const titleText = (titleEl && titleEl.dataset.typewriter) || "";
    const descText = (descEl && descEl.dataset.typewriter) || "";
    typewriter(titleEl, titleText, 45, function () {
      typewriter(descEl, descText, 12);
    });
  }

  // ----- Character counter (Feature 3) -----------------------------------

  function updateCharCounter() {
    if (!charCounter) {
      return;
    }
    const len = input.value.length;
    charCounter.textContent = len + " / " + CHAR_CAP;
    if (len > CHAR_CAP) {
      charCounter.classList.add("over-cap");
    } else {
      charCounter.classList.remove("over-cap");
    }
  }

  // ----- Run stats HUD (Feature 4) ---------------------------------------

  /** Format a millisecond duration as mm:ss. */
  function formatElapsed(ms) {
    const totalSec = Math.floor(ms / 1000);
    const mm = String(Math.floor(totalSec / 60)).padStart(2, "0");
    const ss = String(totalSec % 60).padStart(2, "0");
    return mm + ":" + ss;
  }

  function startElapsedTimer() {
    runStartMs = Date.now();
    if (statElapsed) {
      statElapsed.textContent = "00:00";
    }
    stopElapsedTimer();
    elapsedTimer = window.setInterval(function () {
      if (statElapsed) {
        statElapsed.textContent = formatElapsed(Date.now() - runStartMs);
      }
    }, 1000);
  }

  function stopElapsedTimer() {
    if (elapsedTimer) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  /** Set the current phase label with optional tint class. */
  function setPhase(label, tintClass) {
    if (!statPhase) {
      return;
    }
    statPhase.textContent = label;
    statPhase.classList.remove("phase-complete", "phase-partial", "phase-failed");
    if (tintClass) {
      statPhase.classList.add(tintClass);
    }
  }

  /** Recompute progress bar + rail fill + counters from current tallies. */
  function refreshProgress() {
    const settled = doneCount + failedCount;
    const pct = totalSteps > 0 ? Math.round((settled / totalSteps) * 100) : 0;
    if (progressFill) {
      progressFill.style.width = pct + "%";
    }
    if (progressLabel) {
      progressLabel.textContent = pct + "%";
    }
    if (runProgress) {
      runProgress.setAttribute("aria-valuenow", String(pct));
    }
    if (timelineRailFill) {
      timelineRailFill.style.height = pct + "%";
    }
    if (statDone) statDone.textContent = String(doneCount);
    if (statFailed) statFailed.textContent = String(failedCount);
    if (statTotal) statTotal.textContent = String(totalSteps);
  }

  /** Reset the stats HUD to its standby state for a fresh run. */
  function resetStats() {
    totalSteps = 0;
    doneCount = 0;
    failedCount = 0;
    stepStartTimes = new Map();
    stopElapsedTimer();
    setPhase("STANDBY");
    if (statElapsed) statElapsed.textContent = "00:00";
    if (statRunId) statRunId.textContent = "\u2014";
    currentRunId = "";
    refreshProgress();
  }

  /** Set the short run id in the HUD and remember the full id for copying. */
  function setRunId(runId) {
    currentRunId = runId || "";
    if (statRunId) {
      statRunId.textContent = currentRunId
        ? currentRunId.slice(0, 8)
        : "\u2014";
    }
  }

  // ----- Reset -----------------------------------------------------------

  /** Reset all run-scoped UI state before starting a new run. */
  function resetRunState() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    runFinished = false;
    stepRows = new Map();
    timelineList.innerHTML = "";
    assumptionsList.innerHTML = "";
    resultSteps.innerHTML = "";
    reasoningOutput.textContent = "";
    resultSummary.textContent = "";
    resultStatus.textContent = "";
    resultStatus.className = "result-status";
    resultCard.classList.remove("burst");
    downloadButton.hidden = true;
    downloadButton.removeAttribute("href");
    if (openTabButton) {
      openTabButton.hidden = true;
      openTabButton.removeAttribute("href");
    }
    assumptionsPanel.hidden = true;
    resultCard.hidden = true;
    if (timelineRailFill) {
      timelineRailFill.style.height = "0%";
    }
    resetStats();
  }

  /** Return the console to the idle state so the user can run again. */
  function resetToIdle() {
    resetRunState();
    timeline.hidden = true;
    reasoningLog.hidden = true;
    if (runStats) {
      runStats.hidden = true;
    }
    clearError();
    setFormDisabled(false);
    input.value = "";
    updateCharCounter();
    input.focus();
  }

  /** Render the timeline rows from a plan's steps, each pending. */
  function renderTimeline(steps) {
    timelineList.innerHTML = "";
    stepRows = new Map();
    (steps || []).forEach(function (step) {
      const li = document.createElement("li");
      li.className = "timeline-step";
      li.dataset.step = String(step.step);
      setStepState(li, step.status || "pending");

      const badge = document.createElement("span");
      badge.className = "step-badge";
      li.appendChild(badge);

      const body = document.createElement("div");
      body.className = "step-body";

      const title = document.createElement("div");
      title.className = "step-title";
      title.textContent = step.step + ". " + (step.task || "");
      body.appendChild(title);

      const desc = document.createElement("div");
      desc.className = "step-description";
      desc.textContent = step.description || "";
      body.appendChild(desc);

      const detail = document.createElement("div");
      detail.className = "step-detail";
      detail.hidden = true;
      body.appendChild(detail);

      li.appendChild(body);
      timelineList.appendChild(li);
      stepRows.set(Number(step.step), li);
    });
    totalSteps = stepRows.size;
    refreshProgress();
  }

  /** Apply a status to a timeline row element. */
  function setStepState(row, state) {
    row.dataset.state = state;
    row.classList.remove(
      "state-pending",
      "state-running",
      "state-done",
      "state-failed",
      "state-skipped"
    );
    row.classList.add("state-" + state);
  }

  /** Attach a per-step duration chip once a step settles. */
  function addStepDuration(row, stepNumber) {
    const started = stepStartTimes.get(Number(stepNumber));
    if (!started || !row) {
      return;
    }
    const seconds = Math.max(0, (Date.now() - started) / 1000);
    let chip = row.querySelector(".step-duration");
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "step-duration";
      const body = row.querySelector(".step-body");
      (body || row).appendChild(chip);
    }
    chip.textContent = seconds.toFixed(1) + "s";
  }

  /** Update a single step row's state and optional detail text. */
  function updateStep(stepNumber, state, detailText) {
    const row = stepRows.get(Number(stepNumber));
    if (!row) {
      return;
    }
    setStepState(row, state);
    if (detailText) {
      const detail = row.querySelector(".step-detail");
      if (detail) {
        detail.textContent = detailText;
        detail.hidden = false;
      }
    }
  }

  /** Populate and reveal the assumptions panel when non-empty (Req 11.6). */
  function renderAssumptions(assumptions) {
    if (!assumptions || assumptions.length === 0) {
      return;
    }
    assumptionsList.innerHTML = "";
    assumptions.forEach(function (text) {
      const li = document.createElement("li");
      li.textContent = text;
      assumptionsList.appendChild(li);
    });
    assumptionsPanel.hidden = false;
  }

  /**
   * Render the final result card (Req 11.5, 11.7). Any step still pending at
   * run end is marked "skipped" with a "not executed" indicator.
   */
  function renderResult(status, summary, documentUrl) {
    // Mark still-pending steps as skipped (not executed).
    stepRows.forEach(function (row) {
      const state = row.dataset.state;
      if (state === "pending" || state === "running") {
        setStepState(row, "skipped");
        const detail = row.querySelector(".step-detail");
        if (detail) {
          detail.textContent = "not executed";
          detail.hidden = false;
        }
      }
    });

    resultStatus.textContent = "Status: " + status;
    resultStatus.className = "result-status status-" + status;
    resultSummary.textContent = summary || "";

    // Per-step detail (important for the partial status, Req 11.7).
    resultSteps.innerHTML = "";
    if (status === "partial" || status === "failed") {
      stepRows.forEach(function (row) {
        const title = row.querySelector(".step-title");
        const badge = document.createElement("span");
        badge.className = "result-step-badge state-" + row.dataset.state;
        badge.textContent =
          (title ? title.textContent : "step") + " — " + row.dataset.state;
        resultSteps.appendChild(badge);
      });
    }

    if (documentUrl) {
      downloadButton.href = documentUrl;
      downloadButton.hidden = false;
      if (openTabButton) {
        openTabButton.href = documentUrl;
        openTabButton.hidden = false;
      }
    } else {
      downloadButton.hidden = true;
      downloadButton.removeAttribute("href");
      if (openTabButton) {
        openTabButton.hidden = true;
        openTabButton.removeAttribute("href");
      }
    }

    resultCard.hidden = false;

    // One-time amber glow burst on completion (reduced-motion: skipped).
    if (!reduceMotion) {
      resultCard.classList.remove("burst");
      // Force reflow so the animation re-triggers on subsequent runs.
      void resultCard.offsetWidth;
      resultCard.classList.add("burst");
    }
  }

  // ----- SSE event handling ----------------------------------------------

  /** Dispatch a parsed SSE event payload by its `type`. */
  function handleEvent(data) {
    switch (data.type) {
      case "planning_started":
        setPhase("PLANNING");
        log("Planning…", "planning");
        break;
      case "plan_created":
        setPhase("EXECUTING");
        log("Plan created with " + (data.plan.steps || []).length + " steps.", "planning");
        renderTimeline(data.plan.steps);
        renderAssumptions(data.assumptions || (data.plan && data.plan.assumptions));
        break;
      case "step_started":
        stepStartTimes.set(Number(data.step), Date.now());
        log("Step " + data.step + " started: " + (data.task || ""), "muted");
        updateStep(data.step, "running");
        break;
      case "step_completed":
        doneCount += 1;
        log("Step " + data.step + " completed: " + (data.output_summary || ""), "done");
        updateStep(data.step, "done", data.output_summary);
        addStepDuration(stepRows.get(Number(data.step)), data.step);
        refreshProgress();
        break;
      case "step_failed":
        failedCount += 1;
        log("Step " + data.step + " FAILED: " + (data.error || ""), "failed");
        updateStep(data.step, "failed", data.error);
        addStepDuration(stepRows.get(Number(data.step)), data.step);
        refreshProgress();
        break;
      case "reflection":
        setPhase("REFLECTING");
        log("Reflection: " + (data.findings || ""), "reflection");
        if (data.revised_sections && data.revised_sections.length) {
          log("Revised sections: " + data.revised_sections.join(", "), "reflection");
        }
        break;
      case "run_completed":
        log("Run completed with status: " + data.status, "done");
        finalizePhase(data.status);
        renderResult(data.status, data.summary, data.document_url);
        showToast("Run completed", "Status: " + data.status,
          data.status === "failed" ? "error" : "success");
        finishRun();
        break;
      default:
        log("Event: " + data.type, "muted");
        break;
    }
  }

  /** Map a terminal run status to the final phase label + tint. */
  function finalizePhase(status) {
    if (status === "completed") {
      setPhase("COMPLETE", "phase-complete");
    } else if (status === "partial") {
      setPhase("PARTIAL", "phase-partial");
    } else if (status === "failed") {
      setPhase("FAILED", "phase-failed");
    } else {
      setPhase(String(status || "DONE").toUpperCase());
    }
  }

  /** Close the stream and re-enable the form once the run is done. */
  function finishRun() {
    if (runFinished) {
      return;
    }
    runFinished = true;
    stopElapsedTimer();
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    setFormDisabled(false);
  }

  /** Open the SSE stream for a run and wire up event handling. */
  function openStream(runId, fallbackResponse) {
    eventSource = new EventSource("/agent/" + encodeURIComponent(runId) + "/stream");

    eventSource.onmessage = function (evt) {
      try {
        handleEvent(JSON.parse(evt.data));
      } catch (err) {
        log("Failed to parse event: " + err, "failed");
      }
    };

    eventSource.onerror = function () {
      // The stream closes after the terminal event; if the run already
      // finished this is expected. Otherwise fall back to the POST response.
      if (!runFinished) {
        if (fallbackResponse) {
          finalizePhase(fallbackResponse.status);
          renderResult(
            fallbackResponse.status,
            fallbackResponse.summary,
            fallbackResponse.document_url
          );
          showToast("Run completed", "Status: " + fallbackResponse.status,
            fallbackResponse.status === "failed" ? "error" : "success");
        }
        finishRun();
      }
    };
  }

  // ----- Submit flow ------------------------------------------------------

  async function submitRequest(requestText) {
    clearError();
    resetRunState();
    setFormDisabled(true);

    // Reveal the timeline + stats so their appearance signals a run (Req 11.3).
    timeline.hidden = false;
    reasoningLog.hidden = false;
    if (runStats) {
      runStats.hidden = false;
    }
    setPhase("PLANNING");
    startElapsedTimer();
    showToast("Run started", "Agent is planning your document.", "info");
    log("Submitting request…", "muted");

    let response;
    try {
      response = await fetch("/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: requestText }),
      });
    } catch (err) {
      showError("Network error: could not reach the agent service.");
      showToast("Network error", "Could not reach the agent service.", "error");
      stopElapsedTimer();
      setPhase("FAILED", "phase-failed");
      return;
    }

    if (!response.ok) {
      await handleErrorResponse(response);
      stopElapsedTimer();
      setPhase("FAILED", "phase-failed");
      return;
    }

    let payload;
    try {
      payload = await response.json();
    } catch (err) {
      showError("The agent returned an unexpected response.");
      showToast("Unexpected response", "The agent returned malformed data.", "error");
      stopElapsedTimer();
      return;
    }

    if (payload.run_id) {
      setRunId(payload.run_id);
    }

    // Render assumptions from the response immediately as a fallback; the
    // plan_created event will refresh them during replay.
    renderAssumptions(payload.assumptions);

    if (payload.run_id) {
      openStream(payload.run_id, payload);
    } else {
      // No run_id to stream; render directly from the response.
      if (payload.plan) {
        renderTimeline(payload.plan.steps);
      }
      finalizePhase(payload.status);
      renderResult(payload.status, payload.summary, payload.document_url);
      showToast("Run completed", "Status: " + payload.status,
        payload.status === "failed" ? "error" : "success");
      finishRun();
    }
  }

  /** Translate a non-2xx POST /agent response into a clear UI message. */
  async function handleErrorResponse(response) {
    let body = null;
    try {
      body = await response.json();
    } catch (err) {
      body = null;
    }

    let message;
    if (response.status === 422) {
      if (body && body.error === "request_rejected") {
        message = body.message || "This request was rejected.";
      } else if (body && body.fields) {
        const fieldMsgs = body.fields
          .map(function (f) {
            return f.field + ": " + f.message;
          })
          .join("; ");
        message = "Invalid request — " + fieldMsgs;
      } else {
        message = "The request was invalid.";
      }
    } else if (response.status === 429) {
      const retryAfter = response.headers.get("Retry-After");
      message =
        "Rate limit reached. Please wait" +
        (retryAfter ? " " + retryAfter + " seconds" : "") +
        " and try again.";
    } else if (response.status === 503) {
      message =
        "The agent could not plan this request right now (all backends failed). Please try again.";
    } else {
      message = "Unexpected error (HTTP " + response.status + ").";
    }
    showError(message);
    showToast("Request error", "HTTP " + response.status, "error");
  }

  // ----- Event wiring -----------------------------------------------------

  chipContainer.addEventListener("click", function (evt) {
    const chip = evt.target.closest(".chip");
    if (!chip) {
      return;
    }
    input.value = chip.dataset.example || chip.textContent.trim();
    updateCharCounter();
    input.focus();
  });

  form.addEventListener("submit", function (evt) {
    evt.preventDefault();
    const text = (input.value || "").trim();
    if (!text) {
      showError("Please describe the document you need.");
      return;
    }
    submitRequest(text);
  });

  // Ctrl/Cmd+Enter submits the form (Feature 3).
  input.addEventListener("keydown", function (evt) {
    if ((evt.ctrlKey || evt.metaKey) && evt.key === "Enter") {
      evt.preventDefault();
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    }
  });

  // Live character counter.
  input.addEventListener("input", updateCharCounter);

  // Copy buttons.
  if (copyLogButton) {
    copyLogButton.addEventListener("click", function () {
      copyToClipboard(reasoningOutput.textContent, copyLogButton, "COPIED");
    });
  }
  if (copyRunIdButton) {
    copyRunIdButton.addEventListener("click", function () {
      copyToClipboard(currentRunId, copyRunIdButton, "COPIED");
    });
  }
  if (copySummaryButton) {
    copySummaryButton.addEventListener("click", function () {
      copyToClipboard(resultSummary.textContent, copySummaryButton, "Copied");
    });
  }
  if (newRunButton) {
    newRunButton.addEventListener("click", function () {
      resetToIdle();
    });
  }

  // ----- Boot ------------------------------------------------------------

  updateCharCounter();
  bootReveal();
  refreshBackendStatus();
  window.setInterval(refreshBackendStatus, 10000);
})();
