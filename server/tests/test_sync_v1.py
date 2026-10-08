import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from knowledge_node import auth
from knowledge_node.app import app
from knowledge_node.sync_api import SyncEvent

PROTOCOL_ROOT = Path(__file__).resolve().parents[2] / "protocol"


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def peer_headers(key, device_id, method, path, body=b"", nonce=None):
    timestamp = str(int(time.time()))
    nonce = nonce or str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()
    signature = key.sign(auth.canonical_request(method, path, timestamp, nonce, digest))
    return {
        "X-Knowledge-Device": device_id,
        "X-Knowledge-Timestamp": timestamp,
        "X-Knowledge-Nonce": nonce,
        "X-Knowledge-Content-SHA256": digest,
        "X-Knowledge-Signature": b64(signature),
    }


def pair_test_peer(client, database):
    key = Ed25519PrivateKey.generate()
    public_key = b64(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ))
    offer = client.post("/pairing/offers", json={
        "advertised_url": "http://192.168.1.10:8765", "name": "Test server",
    })
    assert offer.status_code == 200
    response = client.post("/pairing/complete", json={
        "device_id": "android-test", "name": "Test phone",
        "public_key": public_key, "code": offer.json()["code"],
    })
    assert response.status_code == 200
    assert database.peer("android-test")["public_key"] == public_key
    return key


def test_protocol_fixture_is_accepted(database):
    fixture = json.loads((PROTOCOL_ROOT / "fixtures" / "events.json").read_text(encoding="utf-8"))
    parsed = [SyncEvent.model_validate(event) for event in fixture["events"]]
    by_sequence = {event.seq: event for event in parsed}

    observed = []
    for sequence in fixture["delivery_order"]:
        database.ingest_event(by_sequence[sequence].storage_dict())
        observed.append(database.vector_clock())

    assert observed == fixture["expected_clocks"]


def test_authentication_conformance_vector():
    fixture = json.loads((PROTOCOL_ROOT / "fixtures" / "auth-signature.json").read_text(encoding="utf-8"))
    body = fixture["body"].encode("utf-8")
    assert hashlib.sha256(body).hexdigest() == fixture["content_sha256"]
    canonical = auth.canonical_request(
        fixture["method"], fixture["path"], fixture["timestamp"], fixture["nonce"],
        fixture["content_sha256"],
    )
    assert canonical.decode("utf-8") == (PROTOCOL_ROOT / "fixtures" / "auth-canonical.txt").read_text(encoding="utf-8").rstrip("\r\n")
    Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(fixture["public_key"] + "=")) \
        .verify(base64.urlsafe_b64decode(fixture["signature"] + "=="), canonical)


def test_pairing_signature_and_replay_protection(database):
    with TestClient(app) as client:
        key = pair_test_peer(client, database)
        nonce = str(uuid.uuid4())
        headers = peer_headers(key, "android-test", "GET", "/sync/v1/clock", nonce=nonce)

        first = client.get("/sync/v1/clock", headers=headers)
        replay = client.get("/sync/v1/clock", headers=headers)

    assert first.status_code == 200
    assert first.json()["protocol_version"] == "1.0"
    assert replay.status_code == 409


def test_paginated_event_query_uses_signed_exact_body(database):
    for number in range(3):
        database.add_event("note", f"entity-{number}", datetime.now(timezone.utc).isoformat(), {"title": str(number)})
    with TestClient(app) as client:
        key = pair_test_peer(client, database)
        body = json.dumps({"clock": {}, "limit": 2}, separators=(",", ":")).encode("utf-8")
        headers = {
            **peer_headers(key, "android-test", "POST", "/sync/v1/events/query", body),
            "Content-Type": "application/json",
        }
        first = client.post("/sync/v1/events/query", content=body, headers=headers)
        assert first.status_code == 200
        assert len(first.json()["events"]) == 2
        assert first.json()["next_cursor"] is not None

        body = json.dumps({"clock": {}, "limit": 2, "cursor": first.json()["next_cursor"]},
                          separators=(",", ":")).encode("utf-8")
        headers = {
            **peer_headers(key, "android-test", "POST", "/sync/v1/events/query", body),
            "Content-Type": "application/json",
        }
        second = client.post("/sync/v1/events/query", content=body, headers=headers)
        assert len(second.json()["events"]) == 1
        assert second.json()["next_cursor"] is None


def test_resumable_blob_upload(database):
    data = b"abcdefghij"
    digest = hashlib.sha256(data).hexdigest()
    with TestClient(app) as client:
        key = pair_test_peer(client, database)
        for start, chunk in ((0, data[:4]), (4, data[4:])):
            end = start + len(chunk) - 1
            path = f"/sync/v1/blobs/{digest}"
            headers = {
                **peer_headers(key, "android-test", "PUT", path, chunk),
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Content-Type": "application/octet-stream",
            }
            response = client.put(path, content=chunk, headers=headers)
            assert response.status_code == 200

    assert response.json()["complete"] is True
    assert database.blob_path(digest).read_bytes() == data
