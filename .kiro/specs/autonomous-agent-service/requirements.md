# Requirements Document

## Introduction

The Autonomous Agent Service is a locally runnable, production-grade AI system that accepts a natural-language business request, autonomously plans a task list, executes each step through tool calling, performs a self-check (reflection) pass, and produces a polished Microsoft Word (.docx) deliverable. The service exposes a FastAPI backend and a dark "mission control" single-page web console that visualizes the agent's reasoning in real time via server-sent events.

The system MUST operate with zero paid API keys: it uses the Groq free tier (model `llama-3.3-70b-versatile`) by default and automatically falls back to a local Ollama backend when no Groq API key is configured. The core architecture follows a clean Planner → Executor → Reflector loop, with each responsibility isolated in its own class. Mandatory engineering improvements are multi-step planning (primary), plus reflection/self-check and retry/fallback (bonus).

This document defines the testable requirements, user stories, and acceptance criteria for the complete, functional deliverable.

## Glossary

- **Agent_Service**: The overall backend system that orchestrates validation, planning, execution, reflection, and document generation.
- **API_Layer**: The FastAPI application exposing HTTP and SSE endpoints under `app/api/`.
- **Guardrail_Validator**: The component that validates incoming requests via Pydantic schema checks and an LLM-based intent classification.
- **Planner**: The component that produces a structured, multi-step JSON plan from a validated request.
- **Executor**: The component that iterates over the plan and executes each step through registered Tools.
- **Reflector**: The component that reviews generated output against the original request and revises weak sections.
- **LLM_Service**: The abstraction in `app/services/llm.py` that performs LLM calls, retries, JSON repair, and backend selection.
- **Groq_Backend**: The Groq free-tier LLM provider using model `llama-3.3-70b-versatile` via the groq SDK. This is the primary backend.
- **Ollama_Backend**: The local Ollama LLM provider used as automatic fallback. This is the secondary backend.
- **Document_Builder**: The reusable `DocumentBuilder` class in `app/services/docx_builder.py` that generates .docx files via python-docx.
- **Web_Console**: The dark "mission control" single-page web UI.
- **Config_Loader**: The component that loads configuration from environment variables.
- **Run**: A single end-to-end processing of one request, identified by a `run_id`.
- **Run_Id**: A unique identifier assigned to each Run.
- **Plan_Step**: A single unit of the plan containing a step number, task name, description, and expected output.
- **Tool**: A registered callable invoked by the Executor (for example `research`, `draft_section`, `generate_table_data`, `build_docx`).
- **SSE_Stream**: The server-sent-events channel that streams live agent events for a Run.
- **Assumption**: An explicitly stated decision the Planner makes when a request is ambiguous.
- **Active_Backend**: The LLM backend (Groq_Backend or Ollama_Backend) currently selected for use.
- **Document_Url**: The URL path (`document_url`) from which a generated .docx file can be downloaded.
- **security_event**: A structured log entry emitted when a request is rejected as malicious, containing the timestamp, client IP, a hash of the request, and the rejection reason, and never the verbatim malicious payload.
- **derive_status**: The single pure function `derive_status(steps, artifact_exists, summary)` that computes the final Run status from Run state exactly once at Run end.
- **Run_Status**: The value describing the outcome of a Run; one of `pending`, `running`, `completed`, `partial`, or `failed`.
- **Readiness_Endpoint**: The optional `/health/ready` endpoint that reports whether an LLM backend is reachable.
- **Theme_Color**: The environment-variable-defined color applied by the Document_Builder to styled headings, with a documented default.

## Requirements

### Requirement 1: Request Validation and Guardrails

**User Story:** As a business user, I want the service to validate and screen my request before processing, so that malformed or inappropriate requests are rejected quickly with helpful guidance.

#### Acceptance Criteria

1. WHEN a POST request is received at `/agent`, THE API_Layer SHALL validate the request body against a Pydantic schema that requires a non-empty `request` string.
2. IF the request body fails schema validation, THEN THE API_Layer SHALL return HTTP status 422 with a structured error identifying the invalid fields.
3. WHEN a schema-valid request is received, THE Guardrail_Validator SHALL classify the request intent through an LLM call as exactly one of `valid_document_request`, `malicious`, or `non_document`.
4. IF the Guardrail_Validator classifies a request as `malicious` or `non_document`, THEN THE API_Layer SHALL return HTTP status 422 with a message that explains the rejection reason and states that the service produces document deliverables.
5. IF a request is rejected as `malicious`, THEN THE Agent_Service SHALL emit a structured `security_event` log entry containing the timestamp, the client IP, a hash of the request, and the rejection reason, and SHALL record the request hash in place of the verbatim malicious payload.
6. WHILE the number of requests from a single client IP within a 60-second sliding window is at or above 10, THE API_Layer SHALL reject further requests from that client IP with HTTP status 429 and a `Retry-After` header.
7. IF the `Retry-After` header value cannot be generated, THEN THE API_Layer SHALL still reject the over-limit request with HTTP status 429.

### Requirement 2: Autonomous Multi-Step Planning

**User Story:** As a business user, I want the agent to break my request into an ordered set of steps, so that the deliverable is produced through a transparent, structured process.

#### Acceptance Criteria

1. WHEN a validated request is received, THE Planner SHALL produce a plan containing at least two Plan_Steps, where each Plan_Step includes a step number, a task name, a description, and an expected output.
2. THE Planner SHALL return the plan as strict JSON conforming to a Pydantic schema.
3. WHEN a validated request is ambiguous, THE Planner SHALL enumerate each Assumption it makes and record the Assumptions in the Run's `assumptions` list.
4. IF the Planner's LLM call fails or returns unparseable output, THEN THE Planner SHALL retry up to 3 times using exponential backoff and SHALL attempt JSON repair on malformed responses before each retry.
5. IF all retries against the primary backend (Groq_Backend) fail, THEN THE Planner SHALL fall back to the secondary backend (Ollama_Backend) and SHALL repeat the plan-generation attempt.
6. IF plan generation fails on all backends, THEN THE API_Layer SHALL return HTTP status 503 with a structured error body containing the Run_Id, a human-readable failure reason, and the retry history, and THE Agent_Service SHALL log the failure as a structured event.

### Requirement 3: Step Execution via Tool Calling

**User Story:** As a business user, I want the agent to execute each planned step using dedicated tools, so that content is produced systematically and the final document is assembled reliably.

#### Acceptance Criteria

1. WHEN a plan is produced, THE Executor SHALL execute each Plan_Step sequentially through registered Tools.
2. THE Executor SHALL register at minimum the Tools `research(topic)`, `draft_section(title, context)`, `generate_table_data(spec)`, and `build_docx(sections)`.
3. WHEN a Plan_Step begins and when a Plan_Step ends, THE Executor SHALL update the Plan_Step status along the progression `pending → running → done | failed` and SHALL emit the corresponding SSE event.
4. IF a Tool invocation fails after retries, THEN THE Executor SHALL mark that Plan_Step `failed`, record the error, and continue executing the remaining Plan_Steps whose dependencies are satisfied.
5. IF the Executor cannot record an error or cannot determine which remaining Plan_Steps have satisfied dependencies, THEN THE Executor SHALL proceed on a best-effort basis, logging the errors it can record and executing the Plan_Steps it can confirm as safe.

### Requirement 4: Reflection and Self-Check

**User Story:** As a business user, I want the agent to review and improve its own output, so that the final deliverable better matches my original request.

#### Acceptance Criteria

1. WHEN all executable Plan_Steps have finished, THE Reflector SHALL review the assembled output against the original request.
2. IF the Reflector identifies weak or missing sections, THEN THE Reflector SHALL perform at most one revision pass on those sections and SHALL stop after that single pass regardless of any newly identified weak sections.
3. THE Reflector SHALL record its findings and revisions in the Run log and SHALL emit a `reflection` SSE event.

### Requirement 5: LLM Backend Selection and Health

**User Story:** As a developer, I want the service to run with zero paid API keys and pick a working LLM backend automatically, so that I can demo it locally without configuration friction.

#### Acceptance Criteria

1. WHERE `GROQ_API_KEY` is set, THE LLM_Service SHALL select Groq_Backend with model `llama-3.3-70b-versatile` as the Active_Backend.
2. WHERE `GROQ_API_KEY` is not set, THE LLM_Service SHALL fall back automatically to Ollama_Backend as the Active_Backend.
3. WHEN the LLM_Service performs any LLM call, THE LLM_Service SHALL wrap the call with exponential backoff of up to 3 retries and Groq_Backend→Ollama_Backend fallback.
4. WHEN a GET request is received at `/health`, THE API_Layer SHALL return HTTP status 200 with a body indicating service health and the name of the Active_Backend.
5. IF the Active_Backend is not yet resolved when `/health` is requested, THEN THE API_Layer SHALL return HTTP status 200 reporting `"llm_backend": "unknown"`, `"backend_ready": false`, and an explanatory detail string.
6. WHERE the API_Layer exposes the Readiness_Endpoint `/health/ready`, THE Readiness_Endpoint SHALL return HTTP status 503 when no LLM backend is reachable.

### Requirement 6: Live Event Streaming (SSE)

**User Story:** As a demo viewer, I want to watch the agent's progress in real time, so that the autonomous reasoning process is visible as it happens.

#### Acceptance Criteria

1. WHEN a GET request is received at `/agent/{run_id}/stream`, THE API_Layer SHALL open an SSE_Stream that emits the structured events `planning_started`, `plan_created`, `step_started`, `step_completed`, `step_failed`, `reflection`, and `run_completed`.
2. THE API_Layer SHALL include in each SSE event the Run_Id, the event type, a timestamp, and the relevant payload (step data or reasoning snippet).
3. IF the provided `run_id` does not correspond to a known Run, THEN THE stream endpoint SHALL return HTTP status 404 with a structured error without opening an SSE_Stream.

### Requirement 7: Run Status Derivation

**User Story:** As an API consumer, I want the final run status computed deterministically from run state, so that reported outcomes are accurate and never overstate success.

#### Acceptance Criteria

1. THE Run_Status SHALL be exactly one of `pending`, `running`, `completed`, `partial`, or `failed`.
2. THE Agent_Service SHALL compute the final Run_Status exactly once at Run end using the pure function `derive_status(steps, artifact_exists, summary)` and SHALL leave the computed value unmodified thereafter.
3. THE Agent_Service SHALL set the Run_Status to `completed` only when every Plan_Step is `done`, the document artifact exists and is retrievable at `document_url`, and a non-empty summary was generated.
4. IF one or more Plan_Steps end in status `failed`, THEN THE Agent_Service SHALL set the Run_Status to `partial` when a usable document was produced and to `failed` when no usable document exists.
5. IF any Plan_Step ends in status `failed`, THEN THE Agent_Service SHALL set a final Run_Status other than `completed`.

### Requirement 8: Synchronous Response Contract

**User Story:** As an API consumer, I want a single endpoint that returns the full result of an agent Run, so that I can integrate the service programmatically.

#### Acceptance Criteria

1. WHEN a Run finishes, THE API_Layer SHALL return in the `/agent` response the fields `run_id`, `status`, the full `plan` with per-step status and output summaries, `assumptions`, `clarifications_resolved`, `summary`, and `document_url` when an artifact exists.

### Requirement 9: Document Retrieval

**User Story:** As a business user, I want to download the generated Word document, so that I can use the deliverable.

#### Acceptance Criteria

1. WHEN a GET request is received at `/documents/{run_id}.docx` and the document artifact exists, THE API_Layer SHALL return HTTP status 200 with the file bytes and `Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
2. WHILE a document artifact exists for a Run, THE API_Layer SHALL return that file on every retrieval request for the Run.
3. WHEN the API_Layer returns a .docx file, THE API_Layer SHALL set a `Content-Disposition` header providing a download filename derived from the Run_Id.
4. IF the document artifact does not exist, THEN THE API_Layer SHALL return HTTP status 404 with a structured body identifying the reason as `unknown_run`, `in_progress`, or `failed_no_document`.

### Requirement 10: Word Document Quality

**User Story:** As a business user, I want the generated document to look like a real consulting deliverable, so that it is presentable to stakeholders.

#### Acceptance Criteria

1. THE Document_Builder SHALL produce each .docx containing a cover page with the document title, the generation date, and a prepared-by line; a table of contents; styled headings using theme colors; body text; at least one formatted table; at least one bullet list; and page numbers in the footer.
2. THE Agent_Service SHALL encapsulate document assembly in a reusable `DocumentBuilder` class.

### Requirement 11: Mission Control Web Console

**User Story:** As a demo viewer, I want a polished dark agent console, so that submitting a request and watching the agent work is an engaging experience.

#### Acceptance Criteria

1. WHILE no request has been submitted, THE Web_Console SHALL present an idle state containing only a large centered input, example request chips, and a one-line service description.
2. WHILE no request has been submitted, THE Web_Console SHALL keep the Plan_Step timeline hidden so that the timeline's subsequent appearance signals that a Run is underway.
3. WHEN a user submits a request, THE Web_Console SHALL open the SSE_Stream for the Run and SHALL display an animated timeline of Plan_Steps.
4. WHILE a Run is processing, THE Web_Console SHALL render each Plan_Step in its current state (`pending`, `running`, `done`, `failed`, or `skipped`) and SHALL update a Plan_Step's state only in response to the corresponding SSE event.
5. IF a Run terminates while one or more Plan_Steps remain `pending`, THEN THE Web_Console SHALL render those Plan_Steps as `skipped` with a visual indication that they were not executed.
6. WHEN Assumptions exist for a Run, THE Web_Console SHALL display the Assumptions in a dedicated panel.
7. WHEN a Run finishes, THE Web_Console SHALL display a result card containing the summary, the final status including `partial` with per-step detail, and a Download .docx button when an artifact exists.
8. THE Web_Console SHALL use a dark mission-control aesthetic with exactly one accent color, 2–3 neutral colors, a monospace typeface for agent logs, no purple hues, and no gradient fills.

### Requirement 12: Predefined Test Scenarios

**User Story:** As an evaluator, I want the console preloaded with representative test inputs, so that I can immediately exercise both a concrete and an ambiguous request.

#### Acceptance Criteria

1. THE Web_Console SHALL include as example chips (a) "Create a project proposal for migrating our on-premise CRM to the cloud." and (b) the ambiguous leadership-meeting request.
2. WHEN the ambiguous leadership-meeting request is submitted, THE Agent_Service SHALL autonomously choose the document type and SHALL state its Assumptions.

### Requirement 13: Architecture and Code Quality

**User Story:** As a maintainer, I want a clean layered codebase with full typing and documentation, so that the service is easy to understand and extend.

#### Acceptance Criteria

1. THE Agent_Service SHALL organize the codebase into the layers `app/api/`, `app/agent/` (containing `planner.py`, `executor.py`, `reflector.py`, and `tools.py`), `app/services/llm.py`, `app/services/docx_builder.py`, and `app/models/schemas.py`.
2. THE Agent_Service SHALL include full type hints and docstrings on all code.
3. THE README SHALL include setup instructions, an architecture diagram, both predefined test inputs, and an explanation of the implemented engineering improvements.

### Requirement 14: Configuration via Environment Variables

**User Story:** As a developer, I want all configuration to come from environment variables with a documented example, so that setup is reproducible.

#### Acceptance Criteria

1. THE Config_Loader SHALL read all service configuration from environment variables with documented defaults, including at minimum `GROQ_API_KEY`, the Ollama base URL, model names, and server host and port.
2. THE Agent_Service SHALL include a `.env.example` file that lists, unconditionally, every environment variable the service reads, each with a safe placeholder value and a purpose comment.
3. WHERE an environment variable defines a Theme_Color, THE Document_Builder SHALL apply that color to styled headings.
4. IF the Theme_Color variable is unset or invalid, THEN THE Document_Builder SHALL apply the documented default Theme_Color, emit a structured warning identifying the invalid value, and continue the Run to completion.
5. THE Document_Builder SHALL determine Theme_Color application solely from the Theme_Color variable, independent of all other configuration.

### Requirement 15: Observability and Structured Logging

**User Story:** As a maintainer, I want structured decision logging that never compromises availability, so that I can trace agent behavior without risking the Run.

#### Acceptance Criteria

1. WHEN the Planner, Executor, or Reflector makes a decision, THE Agent_Service SHALL emit a structured log entry recording the component name, the Run_Id, and the decision.
2. IF emitting a structured log entry fails, THEN THE Agent_Service SHALL continue the decision-making process, SHALL attempt a single best-effort fallback write to stderr, and SHALL suppress the logging error from the calling component so that observability degrades without affecting availability.

### Requirement 16: Concurrent Runs and State Isolation

**User Story:** As an API consumer, I want multiple runs to be processed independently, so that concurrent requests do not corrupt each other's results.

#### Acceptance Criteria

1. WHEN multiple Runs are processed concurrently, THE Agent_Service SHALL isolate the state of each Run by Run_Id.
2. WHEN an SSE_Stream is requested for a Run_Id, THE API_Layer SHALL emit only the events belonging to that Run_Id.
3. WHEN a document is requested for a Run_Id, THE API_Layer SHALL return only the .docx file produced for that Run_Id.
4. IF a request references a Run_Id that does not correspond to a known Run, THEN THE API_Layer SHALL return HTTP status 404 with a structured error distinguishing the unknown Run_Id.
