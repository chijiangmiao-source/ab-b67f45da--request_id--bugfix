"""Request-id identity binding for the generation qualification.

A ``request_id`` that wins the upgrade qualification is bound to the identity
(version, digest, content) of the candidate it first staged:

* a byte-identical retry is an idempotent replay -- it must not rewrite or
  re-verify the pending candidate;
* reusing the id with a *different* version, digest or content is rejected and
  must never clear, replace or re-verify the verified candidate image.
"""
from __future__ import annotations

import base64

from tests.conftest import make_device


def _submit(client, version, request_id, **extra):
    return client.post(
        "/api/devices/dev-1/candidate",
        json={"version": version, "request_id": request_id, **extra},
    )


def test_same_request_id_cannot_replace_verified_candidate(client):
    make_device(client, version="1.0.0")
    first = _submit(client, "2.0.0", "retry-1")
    assert first.status_code == 200
    assert first.json()["outcome"] == "staged"
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    digest_v2 = dev["slots"]["B"]["digest"]

    # Same request_id, different version/content -> rejected, nothing changes.
    r = _submit(client, "3.0.0", "retry-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_identity_mismatch"

    dev = client.get("/api/devices/dev-1").json()["device"]
    slot_b = dev["slots"]["B"]
    assert slot_b["status"] == "VERIFIED"
    assert slot_b["version"] == "2.0.0"
    assert slot_b["digest"] == digest_v2
    assert slot_b["actual_digest"] == digest_v2
    assert dev["qualified_request"] == "retry-1"
    assert dev["active_slot"] == "A"
    assert dev["slots"]["A"]["version"] == "1.0.0"

    # The preserved candidate is still confirmable and boots as 2.0.0.
    sw = client.post("/api/devices/dev-1/confirm", json={})
    assert sw.status_code == 200
    assert sw.json()["outcome"] == "switched"
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["status"] == "CONFIRMED"


def test_same_request_id_with_altered_content_is_rejected(client):
    # Same version but different bytes/digest is still a different candidate.
    make_device(client, version="1.0.0")
    first = _submit(
        client,
        "2.0.0",
        "retry-1",
        content_b64=base64.b64encode(b"image-v2-" + b"\x01" * 64).decode(),
    )
    assert first.status_code == 200
    digest_v2 = client.get("/api/devices/dev-1").json()["device"]["slots"]["B"]["digest"]

    r = _submit(
        client,
        "2.0.0",
        "retry-1",
        content_b64=base64.b64encode(b"image-v2-" + b"\x02" * 64).decode(),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_identity_mismatch"

    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    assert dev["slots"]["B"]["digest"] == digest_v2


def test_identical_retry_is_idempotent_replay(client):
    make_device(client, version="1.0.0")
    first = _submit(client, "2.0.0", "retry-1")
    assert first.status_code == 200
    before = client.get("/api/devices/dev-1").json()["device"]

    again = _submit(client, "2.0.0", "retry-1")
    assert again.status_code == 200
    assert again.json()["outcome"] == "staged"
    after = client.get("/api/devices/dev-1").json()["device"]

    # The verified candidate is untouched: identical manifest, and no new
    # staging/verification evidence was produced by the replay.
    assert after["slots"]["B"] == before["slots"]["B"]
    assert after["evidence_seq"] == before["evidence_seq"]

    # A different request_id still loses the generation qualification.
    other = _submit(client, "2.0.0", "retry-2")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "upgrade_conflict"


def test_failed_attempt_releases_request_id_for_correction(client):
    make_device(client, version="1.0.0")
    bad = _submit(client, "2.0.0", "retry-1", corrupt=True)
    assert bad.json()["outcome"] == "verification_failed"

    # Qualification was released by the failed verification: the same
    # request_id may stage a corrected image at the same generation.
    good = _submit(client, "2.0.0", "retry-1")
    assert good.status_code == 200
    assert good.json()["outcome"] == "staged"
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    assert dev["slots"]["B"]["version"] == "2.0.0"


def test_interrupted_attempt_can_be_retried_with_same_request_id(client):
    make_device(client, version="1.0.0")
    cut = _submit(client, "2.0.0", "retry-1", fault_point="candidate_write")
    assert cut.json()["outcome"] == "power_cut"

    client.post("/api/devices/dev-1/power-on")
    # The qualification died with the interrupted attempt; an identical retry
    # stages and verifies the candidate from scratch.
    retry = _submit(client, "2.0.0", "retry-1")
    assert retry.status_code == 200
    assert retry.json()["outcome"] == "staged"
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["status"] == "VERIFIED"
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["written"] == dev["slots"]["B"]["size"]
