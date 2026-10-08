from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest


def remote_event(seq, *, event_id=None, entity_id=None, parent_event_id=None, title=None):
    return {
        "device_id": "remote-device",
        "seq": seq,
        "event_id": event_id or f"remote-{seq}",
        "entity_id": entity_id or f"entity-{seq}",
        "kind": "note",
        "created_at": f"2026-01-{seq:02d}T00:00:00+00:00",
        "payload": {"title": title or f"note {seq}", "text": "", "tags": []},
        "parent_event_id": parent_event_id,
    }


def test_clock_does_not_advance_across_a_gap(database):
    database.ingest_event(remote_event(2))
    assert database.vector_clock().get("remote-device", 0) == 0

    database.ingest_event(remote_event(1))
    assert database.vector_clock()["remote-device"] == 2


def test_duplicate_is_idempotent_but_identity_conflict_is_rejected(database):
    event = remote_event(1)
    assert database.ingest_event(event) is True
    assert database.ingest_event(event) is False

    conflicting = {**event, "payload": {**event["payload"], "title": "changed"}}
    with pytest.raises(database.EventConflictError):
        database.ingest_event(conflicting)


def test_concurrent_local_writes_get_unique_contiguous_sequences(database):
    def add(index):
        return database.add_event(
            "note", f"entity-{index}", "2026-01-01T00:00:00+00:00",
            {"title": str(index), "text": "", "tags": []},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(add, range(20)))

    assert sorted(event["seq"] for event in events) == list(range(1, 21))


def test_sibling_updates_are_exposed_as_conflict(database):
    root = remote_event(1, entity_id="shared", event_id="root")
    left = remote_event(2, entity_id="shared", event_id="left", parent_event_id="root", title="left")
    right = {**remote_event(1, entity_id="shared", event_id="right", parent_event_id="root", title="right"),
             "device_id": "other-device"}
    for event in (root, left, right):
        database.ingest_event(event)

    history = database.entity_history("shared")
    assert history["conflict"] is True
    assert {head["event_id"] for head in history["heads"]} == {"left", "right"}
    assert database.current_entity("shared")["_conflict"] is True

    database.add_event(
        "note", "shared", "2026-02-01T00:00:00+00:00",
        {"title": "merged", "text": "", "tags": [], "_merged_event_ids": ["left", "right"]},
        "left",
    )
    assert database.entity_history("shared")["conflict"] is False
    assert database.current_entity("shared")["title"] == "merged"
    assert "_merged_event_ids" not in database.current_entity("shared")


def test_delete_and_missing_blob_reconciliation(database):
    root = database.add_event("note", "shared", "2026-01-01T00:00:00+00:00", {"title": "x"})
    database.add_event("delete", "shared", "2026-01-02T00:00:00+00:00", {}, root["event_id"])
    assert database.current_entity("shared") is None

    data = b"blob contents"
    digest = hashlib.sha256(data).hexdigest()
    database.ingest_event({
        **remote_event(1, entity_id="document", event_id="document-event"),
        "kind": "document",
        "payload": {"blob": {"sha256": digest, "size_bytes": len(data),
                               "mime_type": "text/plain", "original_name": "a.txt"}},
    })
    assert digest in database.missing_blob_metadata()
    database.store_blob(data, "a.txt", "text/plain")
    assert digest not in database.missing_blob_metadata()
