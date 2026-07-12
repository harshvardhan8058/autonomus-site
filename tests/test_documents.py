"""Property test for document retrieval (Task 17.5, Property 14).

Property 14 — *Document retrieval is idempotent and isolated*: for any run whose
artifact exists, repeated ``GET /documents/{run_id}.docx`` requests return
byte-identical content, the ``Content-Disposition`` filename contains that
``run_id``, and the returned bytes belong only to that run's artifact (never
another run's).

**Validates: Requirements 9.2, 9.3, 16.3**

The test builds an app (via ``tests/support.build_app``) whose
:class:`~app.core.run_store.RunStore` is pre-populated with several finished
runs, each pointing at a distinct real temporary ``.docx`` file with content
unique to that run. It then drives :class:`~starlette.testclient.TestClient`
against the document endpoint. Property tests use Hypothesis at a minimum of 100
iterations.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient

from app.core.run_store import RunStore
from app.models.schemas import RunStatus
from tests.support import build_app

# Run-id alphabet: alphanumeric plus hyphen (matches the documented id format).
_RUN_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

_run_ids = st.text(alphabet=_RUN_ID_ALPHABET, min_size=1, max_size=16)


# Feature: autonomous-agent-service, Property 14: Document retrieval is idempotent and isolated
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    run_ids=st.lists(_run_ids, min_size=1, max_size=4, unique=True),
    payload=st.binary(min_size=0, max_size=48),
)
def test_property_14_document_retrieval_idempotent_and_isolated(
    run_ids: list[str],
    payload: bytes,
) -> None:
    """Property 14: repeated document GETs are idempotent and per-run isolated.

    **Validates: Requirements 9.2, 9.3, 16.3**

    For a set of runs each with a distinct on-disk artifact, every run's
    document is served byte-identically across repeated requests, its
    ``Content-Disposition`` filename contains the run id, and the bytes returned
    for one run never equal another run's bytes.
    """

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = RunStore()
        expected: dict[str, bytes] = {}

        for index, run_id in enumerate(run_ids):
            # Make each run's content unique by construction so cross-run
            # isolation can be asserted regardless of the drawn payload.
            content = f"docx::{index}::{run_id}::".encode() + payload
            artifact = tmp_path / f"artifact-{index}.docx"
            artifact.write_bytes(content)

            run_state = store.create(
                run_id, request="Create a deliverable.", client_ip="127.0.0.1"
            )
            run_state.document_path = artifact
            run_state.status = RunStatus.COMPLETED
            store.update(run_state)
            expected[run_id] = content

        app = build_app(run_store=store)
        client = TestClient(app)

        served: dict[str, bytes] = {}
        for run_id, content in expected.items():
            first = client.get(f"/documents/{run_id}.docx")
            second = client.get(f"/documents/{run_id}.docx")

            # Idempotent: both requests succeed with byte-identical content that
            # matches this run's artifact (Req 9.2).
            assert first.status_code == 200
            assert second.status_code == 200
            assert first.content == second.content == content

            # The Content-Disposition filename contains the run id (Req 9.3).
            disposition = first.headers.get("content-disposition", "")
            assert run_id in disposition

            served[run_id] = first.content

        # Isolation: each run's served bytes equal only its own artifact and no
        # other run's, so a document request returns only that run's file
        # (Req 16.3).
        for run_id, content in served.items():
            assert content == expected[run_id]
            for other_id, other_content in expected.items():
                if other_id != run_id:
                    assert content != other_content
