"""Document retrieval endpoint: ``GET /documents/{run_id}.docx`` (Req 9, 16.3, 16.4).

This module exposes the endpoint that serves a run's generated Word deliverable.
When the artifact exists it is streamed back with the correct ``.docx``
``Content-Type`` and a ``Content-Disposition`` filename derived from the run id
(Req 9.1, 9.2, 9.3). Otherwise a structured HTTP 404 is returned whose ``reason``
distinguishes the three failure cases (Req 9.4, 16.4):

- ``unknown_run`` — no run is registered under the requested id;
- ``in_progress`` — the run exists but is still pending/running with no artifact;
- ``failed_no_document`` — the run finished but produced no usable artifact.

Because the artifact is keyed by run id, a request for one run can only ever
return that run's document (Req 16.3).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

from app.api.deps import get_run_store
from app.core.run_store import RunStore
from app.models.schemas import DocumentNotFoundBody, RunStatus

router = APIRouter(tags=["documents"])

# The MIME type for a Word (.docx) document (Req 9.1).
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Run statuses that indicate the run has not yet finished, so a missing artifact
# means the document is still in progress rather than permanently unavailable.
_IN_PROGRESS_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING})


@router.get("/documents/{run_id}.docx", response_model=None)
async def get_document(
    run_id: str,
    store: RunStore = Depends(get_run_store),
) -> FileResponse | JSONResponse:
    """Serve a run's ``.docx`` deliverable or a structured 404 (Req 9, 16.3, 16.4).

    Resolves the run and returns the artifact when it exists on disk, with the
    ``.docx`` ``Content-Type`` and a ``Content-Disposition`` attachment filename
    derived from ``run_id`` (Req 9.1, 9.2, 9.3). When no usable artifact is
    available, a 404 :class:`DocumentNotFoundBody` is returned whose ``reason``
    is ``unknown_run`` (no such run), ``in_progress`` (run not yet finished), or
    ``failed_no_document`` (run finished without a usable artifact) (Req 9.4,
    16.4).

    Args:
        run_id: The identifier of the run whose document is requested.
        store: The run store (injected from app state).

    Returns:
        A :class:`~fastapi.responses.FileResponse` with the document bytes, or a
        404 :class:`~fastapi.responses.JSONResponse` with the failure reason.
    """

    run_state = store.get(run_id)
    if run_state is None:
        return _not_found("unknown_run")

    document_path = run_state.document_path
    if document_path is not None and _path_exists(document_path):
        return FileResponse(
            path=str(document_path),
            media_type=_DOCX_MEDIA_TYPE,
            filename=f"agent-run-{run_id}.docx",
        )

    # No usable artifact: distinguish "still running" from "finished, none".
    if run_state.status in _IN_PROGRESS_STATUSES:
        return _not_found("in_progress")
    return _not_found("failed_no_document")


def _path_exists(path: object) -> bool:
    """Return whether ``path`` refers to an existing file, never raising.

    Args:
        path: A :class:`pathlib.Path`-like object recorded on the run state.

    Returns:
        ``True`` when the path exists on disk, ``False`` otherwise (including on
        any filesystem error).
    """

    try:
        return path.exists()  # type: ignore[attr-defined]
    except OSError:  # pragma: no cover - defensive filesystem guard
        return False


def _not_found(
    reason: str,
) -> JSONResponse:
    """Build a 404 :class:`DocumentNotFoundBody` response (Req 9.4).

    Args:
        reason: One of ``"unknown_run"``, ``"in_progress"``, or
            ``"failed_no_document"``.

    Returns:
        A 404 :class:`~fastapi.responses.JSONResponse` carrying the reason.
    """

    body = DocumentNotFoundBody(reason=reason)  # type: ignore[arg-type]
    return JSONResponse(status_code=404, content=body.model_dump(mode="json"))
