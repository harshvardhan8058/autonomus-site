"""Deterministic, network-free content generators for offline fallback.

This module provides the last-resort content the agent uses when **every** LLM
backend (Groq and Ollama) is unreachable and all retries are exhausted. The
generators here are pure and deterministic: they perform no network I/O, make no
LLM calls, and derive their output solely from their string inputs so a
meaningful ``.docx`` deliverable is always produced (graceful degradation).

The functions are intentionally generic and qualitative — they never fabricate
precise statistics — and they weave a short topic derived from the request into
their prose so the output reads tailored rather than boilerplate. The
:func:`default_plan` builder returns a fully-valid :class:`~app.models.schemas.Plan`
that satisfies the schema validators (at least two sequential steps), and it
records an honest assumption noting that the content was produced from built-in
templates because no live LLM backend was reachable.
"""

from __future__ import annotations

from app.models.schemas import Plan, PlanStep

# The maximum number of characters retained for a topic derived from a request.
_TOPIC_MAX_CHARS = 80

# The honest disclosure recorded on every offline-generated plan's assumptions so
# the degraded-mode origin of the content is always traceable and reviewable.
_OFFLINE_NOTICE = (
    "No live LLM backend was reachable, so this document was generated from the "
    "service's built-in deterministic templates; please review and refine the "
    "content."
)


def extract_topic(request: str) -> str:
    """Derive a concise topic string from a business ``request``.

    Takes the first clause of the first line (splitting on common clause
    separators), trims surrounding whitespace, collapses internal whitespace, and
    truncates to a reasonable length. Falls back to a generic label when the
    request yields no usable text.

    Args:
        request: The natural-language business request.

    Returns:
        A short, single-line topic string suitable for weaving into prose.
    """

    text = " ".join((request or "").split())
    if not text:
        return "the requested business document"
    # Prefer the first clause, breaking on the earliest common separator.
    for separator in (".", ";", ",", ":", " - ", " for ", " that "):
        index = text.find(separator)
        if 0 < index < _TOPIC_MAX_CHARS:
            text = text[:index]
            break
    text = text.strip()
    if len(text) > _TOPIC_MAX_CHARS:
        text = text[: _TOPIC_MAX_CHARS - 1].rstrip() + "\u2026"
    return text or "the requested business document"


def default_plan(request: str) -> Plan:
    """Build a coherent, valid multi-step :class:`Plan` without any LLM call.

    The plan researches the topic, drafts three core sections, generates a
    comparison table, and assembles the final ``.docx``. Every step carries the
    real tool names the executor dispatches on (``research``, ``draft_section``,
    ``generate_table_data``, ``build_docx``) and a professional section heading.
    A short topic derived from ``request`` is woven into the section titles and
    descriptions so the plan reads tailored to the request.

    The returned plan satisfies the :class:`Plan` schema validators (at least two
    sequential steps starting at 1). Its ``assumptions`` open with an honest note
    that the content was produced from built-in templates because no live LLM
    backend was reachable, followed by a couple of reasonable assumptions derived
    from the request.

    Args:
        request: The natural-language business request the plan addresses.

    Returns:
        A validated :class:`Plan` ready to drive the executor offline.
    """

    topic = extract_topic(request)
    steps = [
        PlanStep(
            step=1,
            task="research",
            section_title="Background and Context",
            description=f"Gather background and key considerations about {topic}.",
            expected_output="A concise briefing of relevant facts and considerations.",
        ),
        PlanStep(
            step=2,
            task="draft_section",
            section_title="Overview",
            description=f"Draft an overview section introducing {topic}.",
            expected_output="An introductory overview section.",
        ),
        PlanStep(
            step=3,
            task="draft_section",
            section_title="Key Considerations",
            description=f"Draft a section outlining key considerations for {topic}.",
            expected_output="A section detailing considerations and trade-offs.",
        ),
        PlanStep(
            step=4,
            task="draft_section",
            section_title="Recommendations",
            description=f"Draft a recommendations section for {topic}.",
            expected_output="A section with actionable recommendations.",
        ),
        PlanStep(
            step=5,
            task="generate_table_data",
            section_title="Option Comparison",
            description=f"Produce a qualitative comparison table relevant to {topic}.",
            expected_output="A comparison table of options and cost considerations.",
        ),
        PlanStep(
            step=6,
            task="build_docx",
            section_title="",
            description="Assemble the drafted sections into the final .docx deliverable.",
            expected_output="A downloadable Word document.",
        ),
    ]
    assumptions = [
        _OFFLINE_NOTICE,
        f"Assumed the deliverable concerns {topic}.",
        "Assumed a general professional audience for the document.",
    ]
    return Plan(steps=steps, assumptions=assumptions)


def _research_subject(topic: str) -> str:
    """Extract a concise subject from a possibly-imperative ``topic`` string.

    When ``topic`` is a full instruction such as
    ``"Gather background and key considerations about X."`` the trailing subject
    after the first ``" about "`` / ``" on "`` / ``" for "`` connector is used so
    the briefing reads "Briefing on X" rather than echoing the whole instruction.
    Otherwise the cleaned ``topic`` is returned. Deterministic and safe for
    arbitrary input.

    Args:
        topic: The subject or instruction to brief.

    Returns:
        A non-empty, single-line subject string.
    """

    text = " ".join((topic or "").split())
    if not text:
        return "the subject"
    lowered = text.lower()
    for connector in (" about ", " on ", " for "):
        index = lowered.find(connector)
        if index != -1:
            candidate = text[index + len(connector):].strip().rstrip(".;:,")
            if candidate:
                return candidate
    return text.rstrip(".;:,") or "the subject"


def offline_research(topic: str) -> str:
    """Return a generic-but-relevant research briefing about ``topic``.

    The briefing is qualitative prose with a few bullet points; it deliberately
    avoids inventing precise statistics. It is suitable as the researched-facts
    context a drafting step draws from. When ``topic`` is a full imperative
    instruction, the trailing subject is extracted so the briefing reads
    naturally rather than echoing the whole instruction.

    Args:
        topic: The subject to brief.

    Returns:
        A non-empty multi-line briefing string.
    """

    subject = _research_subject(topic)
    return (
        f"Briefing on {subject}:\n"
        f"- {subject} is best approached by first clarifying goals, scope, and "
        "the stakeholders involved.\n"
        "- Established practice favors an incremental approach, validating "
        "assumptions early and managing risk deliberately.\n"
        "- Typical considerations include cost, timeline, required capabilities, "
        "operational impact, and long-term maintainability.\n"
        f"- Weighing the available options for {subject} against these factors "
        "helps identify a balanced, defensible recommendation.\n"
        "Note: figures and specifics should be confirmed against authoritative, "
        "up-to-date sources before publication."
    )


# Section framings keyed by intent. Each entry supplies an opening paragraph
# lead-in and a middle paragraph lead-in, selected by keywords in the heading so
# different section types read distinctly rather than as identical boilerplate.
# The tuples are ``(keywords, opening_lead, middle_lead)`` and are checked in
# order so more specific intents win.
_SECTION_FRAMINGS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("overview", "introduction", "summary"),
        "introduces the deliverable and orients the reader",
        "sets out the scope, the objectives, and the questions the document sets "
        "out to answer",
    ),
    (
        ("recommendation", "next step", "action"),
        "sets out actionable recommendations",
        "prioritizes the proposed actions, notes the rationale behind each, and "
        "flags the decisions that warrant sign-off",
    ),
    (
        ("consideration", "risk", "trade-off", "tradeoff", "challenge"),
        "examines the key trade-offs and risks",
        "weighs the competing factors, surfaces the dependencies and failure "
        "modes, and outlines how each risk might be mitigated",
    ),
    (
        ("background", "context", "history"),
        "establishes the situational background",
        "traces how the current situation arose, identifies the stakeholders "
        "involved, and clarifies the constraints already in play",
    ),
)

# The neutral framing used when a heading matches none of the specific intents.
_DEFAULT_FRAMING = (
    "addresses the topic in the wider context of the deliverable",
    "frames the relevant objectives, highlights the factors most likely to "
    "influence the outcome, and keeps the discussion tied to the stated goals",
)


def _section_framing(heading_lower: str) -> tuple[str, str]:
    """Select an opening/middle framing for a section by its heading keywords.

    Args:
        heading_lower: The lower-cased section heading.

    Returns:
        An ``(opening_lead, middle_lead)`` tuple driving the prose framing.
    """

    for keywords, opening_lead, middle_lead in _SECTION_FRAMINGS:
        if any(keyword in heading_lower for keyword in keywords):
            return opening_lead, middle_lead
    return _DEFAULT_FRAMING


def offline_section(title: str, context: str) -> str:
    """Return professional prose for a document section, offline.

    Produces two to three short paragraphs appropriate for a business document.
    Both the section ``title`` (as a natural heading reference) and the supplied
    ``context`` (which carries the section's intent) shape the prose. A
    section-appropriate framing is selected from keywords in the heading so an
    overview, a recommendations section, a considerations/risks section, and a
    background section each read distinctly rather than as identical boilerplate.
    No hard numbers are invented; the output is deterministic and pure.

    Args:
        title: The section heading the prose is written for.
        context: Supporting context and facts to reference; its intent shapes the
            framing and it is woven into the prose.

    Returns:
        A non-empty section body string.
    """

    heading = (title or "This section").strip() or "This section"
    heading_lower = heading.lower()
    opening_lead, middle_lead = _section_framing(heading_lower)

    context_text = " ".join((context or "").split())
    if context_text:
        context_sentence = (
            f" It draws on the accompanying context — {context_text} — to keep the "
            "discussion grounded in the specifics of the request."
        )
    else:
        context_sentence = (
            " In the absence of additional detail, it stays at a level general "
            "enough to remain useful once organization-specific facts are added."
        )

    return (
        f"The {heading} section {opening_lead}.{context_sentence}\n\n"
        f"Here the treatment of {heading} {middle_lead}. The emphasis is on "
        "clarity and on conclusions that can be justified against the stated "
        "goals rather than on unsupported assertions.\n\n"
        f"These points on {heading} should be reviewed and refined with "
        "organization-specific detail before the document is finalized."
    )


def offline_table(spec: str) -> tuple[list[str], list[list[str]]]:
    """Return a sensible qualitative comparison table for ``spec``, offline.

    Produces a small on-premise vs. cloud style comparison with a few
    qualitative rows. The rows are rectangular and aligned to the headers. The
    content is generic and avoids fabricated precise figures.

    Args:
        spec: A description of the table to generate (used only to keep the
            output loosely relevant; the structure is fixed and deterministic).

    Returns:
        A ``(headers, rows)`` tuple where every row aligns to ``headers``.
    """

    headers = ["Category", "On-Premise", "Cloud"]
    rows = [
        ["Upfront Cost", "Higher", "Lower"],
        ["Ongoing Cost", "Fixed", "Usage-based"],
        ["Scalability", "Limited", "Elastic"],
        ["Maintenance", "In-house", "Provider-managed"],
        ["Time to Deploy", "Longer", "Shorter"],
    ]
    return headers, rows
