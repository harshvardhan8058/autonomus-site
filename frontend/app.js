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

  const assumptionsPanel = document.getElementById("assumptions-panel");
  const assumptionsList = document.getElementById("assumptions-list");

  const reasoningLog = document.getElementById("reasoning-log");
  const reasoningOutput = document.getElementById("reasoning-output");

  const resultCard = document.getElementById("result-card");
  const resultStatus = document.getElementById("result-status");
  const resultSummary = document.getElementById("result-summary");
  const resultSteps = document.getElementById("result-steps");
  const downloadButton = document.getElementById("download-button");

  // ----- Run-scoped state -------------------------------------------------
  // Maps a 1-based step number to its rendered <li> row element.
  let stepRows = new Map();
  let eventSource = null;
  let runFinished = false;

  // ----- Helpers ----------------------------------------------------------

  /** Append a monospace line to the reasoning log for every SSE event. */
  function log(line) {
    const stamp = new Date().toLocaleTimeString();
    reasoningOutput.textContent += `[${stamp}] ${line}\n`;
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
    downloadButton.hidden = true;
    downloadButton.removeAttribute("href");
    assumptionsPanel.hidden = true;
    resultCard.hidden = true;
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
    } else {
      downloadButton.hidden = true;
      downloadButton.removeAttribute("href");
    }

    resultCard.hidden = false;
  }

  // ----- SSE event handling ----------------------------------------------

  /** Dispatch a parsed SSE event payload by its `type`. */
  function handleEvent(data) {
    switch (data.type) {
      case "planning_started":
        log("Planning…");
        break;
      case "plan_created":
        log("Plan created with " + (data.plan.steps || []).length + " steps.");
        renderTimeline(data.plan.steps);
        renderAssumptions(data.assumptions || (data.plan && data.plan.assumptions));
        break;
      case "step_started":
        log("Step " + data.step + " started: " + (data.task || ""));
        updateStep(data.step, "running");
        break;
      case "step_completed":
        log("Step " + data.step + " completed: " + (data.output_summary || ""));
        updateStep(data.step, "done", data.output_summary);
        break;
      case "step_failed":
        log("Step " + data.step + " FAILED: " + (data.error || ""));
        updateStep(data.step, "failed", data.error);
        break;
      case "reflection":
        log("Reflection: " + (data.findings || ""));
        if (data.revised_sections && data.revised_sections.length) {
          log("Revised sections: " + data.revised_sections.join(", "));
        }
        break;
      case "run_completed":
        log("Run completed with status: " + data.status);
        renderResult(data.status, data.summary, data.document_url);
        finishRun();
        break;
      default:
        log("Event: " + data.type);
        break;
    }
  }

  /** Close the stream and re-enable the form once the run is done. */
  function finishRun() {
    if (runFinished) {
      return;
    }
    runFinished = true;
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
        log("Failed to parse event: " + err);
      }
    };

    eventSource.onerror = function () {
      // The stream closes after the terminal event; if the run already
      // finished this is expected. Otherwise fall back to the POST response.
      if (!runFinished) {
        if (fallbackResponse) {
          renderResult(
            fallbackResponse.status,
            fallbackResponse.summary,
            fallbackResponse.document_url
          );
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

    // Reveal the timeline so its appearance signals a run is underway (Req 11.3).
    timeline.hidden = false;
    reasoningLog.hidden = false;
    log("Submitting request…");

    let response;
    try {
      response = await fetch("/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: requestText }),
      });
    } catch (err) {
      showError("Network error: could not reach the agent service.");
      return;
    }

    if (!response.ok) {
      await handleErrorResponse(response);
      return;
    }

    let payload;
    try {
      payload = await response.json();
    } catch (err) {
      showError("The agent returned an unexpected response.");
      return;
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
      renderResult(payload.status, payload.summary, payload.document_url);
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
  }

  // ----- Event wiring -----------------------------------------------------

  chipContainer.addEventListener("click", function (evt) {
    const chip = evt.target.closest(".chip");
    if (!chip) {
      return;
    }
    input.value = chip.dataset.example || chip.textContent.trim();
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
})();
