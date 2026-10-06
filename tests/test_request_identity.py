"""request_id identity binding: idempotent retry vs. identity-mismatch reuse.

A request_id that wins the generation qualification is bound to the candidate
identity (version, digest, content) it first staged:

* a byte-identical retry replays the original outcome and rewrites nothing;
* the same request_id carrying a different version, digest, or content is
  rejected with 409 and must not clear, replace, or re-verify the pending
  candidate;
* a *different* request_id still gets the stable ``upgrade_conflict`` 409.
"""
from __future__ import annotations

import base64
import hashlib

from tests.conftest import make_device


def _submit(client, version, request_id, **extra):
    body = {"version": version, "request_id": request_id}
    body.update(extra)
    return client.post("/api/devices/dev-1/candidate", json=body)


def _device(client):
    return client.get("/api/devices/dev-1").json()["device"]


def test_same_request_id_with_different_version_is_rejected(client):
    """The reported bug: retry-1 stages 2.0.0, then 3.0.0 must not replace it."""
    make_device(client, version="1.0.0")
    first = _submit(client, "2.0.0", "retry-1")
    assert first.status_code == 200
    assert first.json()["outcome"] == "staged"
    digest_before = first.json()["device"]["slots"]["B"]["digest"]

    second = _submit(client, "3.0.0", "retry-1")
    assert second.status_code == 409
    err = second.json()["error"]
    assert err["code"] == "request_identity_mismatch"
    assert err["holder_request"] == "retry-1"
    assert err["bound_version"] == "2.0.0"

    dev = _device(client)
    # B slot is still the verified 2.0.0 candidate -- not cleared, not
    # replaced, not re-verified into something else.
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    assert dev["slots"]["B"]["digest"] == digest_before
    # Active slot and qualification are untouched.
    assert dev["active_slot"] == "A"
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["qualified_request"] == "retry-1"
    assert dev["generation"] == 1


def test_same_request_id_with_different_content_is_rejected(client):
    make_device(client, version="1.0.0")
    assert _submit(client, "2.0.0", "retry-1").status_code == 200

    forged = base64.b64encode(b"forged-image-bytes").decode()
    r = _submit(client, "2.0.0", "retry-1", content_b64=forged)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_identity_mismatch"

    dev = _device(client)
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    assert dev["slots"]["B"]["size"] == 256  # original synthetic image intact


def test_same_request_id_with_different_digest_is_rejected(client):
    make_device(client, version="1.0.0")
    assert _submit(client, "2.0.0", "retry-1").status_code == 200

    other_digest = hashlib.sha256(b"somebody-else").hexdigest()
    r = _submit(client, "2.0.0", "retry-1", digest=other_digest)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_identity_mismatch"

    dev = _device(client)
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["status"] == "VERIFIED"


def test_identical_retry_replays_outcome_without_rewriting(client):
    make_device(client, version="1.0.0")
    first = _submit(client, "2.0.0", "retry-1")
    assert first.status_code == 200
    before = _device(client)

    for _ in range(3):
        again = _submit(client, "2.0.0", "retry-1")
        assert again.status_code == 200
        body = again.json()
        assert body["outcome"] == "staged"
        assert body["idempotent_replay"] is True
        assert body["target_slot"] == "B"

    after = _device(client)
    # Slots, evidence log, qualification, and generation are all unchanged:
    # the retry neither rewrote the image nor appended new staging evidence.
    assert after["slots"] == before["slots"]
    assert after["evidence"] == before["evidence"]
    assert after["evidence_seq"] == before["evidence_seq"]
    assert after["qualified_request"] == "retry-1"
    assert after["generation"] == before["generation"]


def test_different_request_id_still_conflicts_after_binding(client):
    make_device(client, version="1.0.0")
    assert _submit(client, "2.0.0", "retry-1").status_code == 200

    r = _submit(client, "3.0.0", "retry-2")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "upgrade_conflict"
    assert err["holder_request"] == "retry-1"

    dev = _device(client)
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["qualified_request"] == "retry-1"


def test_bound_candidate_remains_confirmable_after_rejected_misuse(client):
    make_device(client, version="1.0.0")
    _submit(client, "2.0.0", "retry-1")
    _submit(client, "3.0.0", "retry-1")  # rejected, candidate untouched

    sw = client.post("/api/devices/dev-1/confirm", json={})
    assert sw.status_code == 200
    assert sw.json()["outcome"] == "switched"
    assert sw.json()["active_slot"] == "B"
    assert sw.json()["generation"] == 2

    dev = _device(client)
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["status"] == "CONFIRMED"
    assert dev["slots"]["A"]["status"] == "SUPERSEDED"


def test_request_id_binding_releases_with_qualification(client):
    """After the switch commits, the next generation may reuse the identifier."""
    make_device(client, version="1.0.0")
    _submit(client, "2.0.0", "retry-1")
    client.post("/api/devices/dev-1/confirm", json={})

    r = _submit(client, "3.0.0", "retry-1")
    assert r.status_code == 200
    assert r.json()["outcome"] == "staged"
    assert r.json()["target_slot"] == "A"
