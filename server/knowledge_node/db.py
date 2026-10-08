from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

ROOT = Path(os.environ.get("KNOWLEDGE_HOME", Path.home() / ".local_knowledge"))
DB_PATH = ROOT / "knowledge.sqlite3"
BLOB_DIR = ROOT / "blobs"
UPLOAD_DIR = ROOT / "uploads"
DEVICE_FILE = ROOT / "device_id.txt"
BLOB_UPLOAD_LOCK = Lock()


class EventConflictError(ValueError):
    """A replicated identity was reused for different event content."""


def ensure_dirs() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def device_id() -> str:
    ensure_dirs()
    if DEVICE_FILE.exists():
        return DEVICE_FILE.read_text(encoding="utf-8").strip()
    value = str(uuid.uuid4())
    try:
        with DEVICE_FILE.open("x", encoding="utf-8") as handle:
            handle.write(value)
        return value
    except FileExistsError:
        return DEVICE_FILE.read_text(encoding="utf-8").strip()


def connect() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                device_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                entity_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                parent_event_id TEXT,
                PRIMARY KEY (device_id, seq)
            );
            CREATE TABLE IF NOT EXISTS device_state (
                device_id TEXT PRIMARY KEY,
                next_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blobs (
                sha256 TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                mime_type TEXT,
                original_name TEXT
            );
            CREATE TABLE IF NOT EXISTS derived_documents (
                entity_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                extractor_id TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (entity_id, source_event_id)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                locator_json TEXT NOT NULL,
                UNIQUE (source_event_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS embeddings (
                chunk_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (chunk_id, model_id),
                FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS peers (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                paired_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_nonces (
                device_id TEXT NOT NULL,
                nonce TEXT NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (device_id, nonce),
                FOREIGN KEY (device_id) REFERENCES peers(device_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pairing_offers (
                code_hash TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id);
            CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_event_id);
            CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_id);
            """
        )
        columns = {row["name"] for row in con.execute("PRAGMA table_info(events)")}
        if "parent_event_id" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN parent_event_id TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_event_id)")


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "device_id": row["device_id"], "seq": int(row["seq"]),
        "event_id": row["event_id"], "entity_id": row["entity_id"],
        "kind": row["kind"], "created_at": row["created_at"],
        "payload": json.loads(row["payload_json"]),
        "parent_event_id": row["parent_event_id"],
    }


def add_event(
    kind: str, entity_id: str, created_at: str, payload: dict[str, Any],
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    did = device_id()
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        if parent_event_id is not None:
            parent = con.execute(
                "SELECT entity_id FROM events WHERE event_id = ?", (parent_event_id,)
            ).fetchone()
            if parent is None or parent["entity_id"] != entity_id:
                raise ValueError("parent event does not exist for this entity")
        row = con.execute(
            "SELECT next_seq FROM device_state WHERE device_id = ?", (did,)
        ).fetchone()
        if row is None:
            maximum = con.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM events WHERE device_id = ?", (did,)
            ).fetchone()
            seq = int(maximum["n"]) + 1
            con.execute("INSERT INTO device_state VALUES(?, ?)", (did, seq + 1))
        else:
            seq = int(row["next_seq"])
            con.execute("UPDATE device_state SET next_seq = ? WHERE device_id = ?", (seq + 1, did))
        event = {
            "device_id": did, "seq": seq, "event_id": str(uuid.uuid4()),
            "entity_id": entity_id, "kind": kind, "created_at": created_at,
            "payload": payload, "parent_event_id": parent_event_id,
        }
        con.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (did, seq, event["event_id"], entity_id, kind, created_at,
             json.dumps(payload, separators=(",", ":"), sort_keys=True), parent_event_id),
        )
        con.commit()
        return event
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _same_event(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    fields = ("device_id", "seq", "event_id", "entity_id", "kind", "created_at", "payload", "parent_event_id")
    return all(existing.get(field) == incoming.get(field) for field in fields)


def ingest_event(event: dict[str, Any]) -> bool:
    """Insert a replicated event, rejecting identity reuse with different content."""
    normalized = {**event, "seq": int(event["seq"]), "parent_event_id": event.get("parent_event_id")}
    with connect() as con:
        existing = con.execute(
            "SELECT * FROM events WHERE event_id = ? OR (device_id = ? AND seq = ?)",
            (normalized["event_id"], normalized["device_id"], normalized["seq"]),
        ).fetchone()
        if existing is not None:
            if _same_event(_event_from_row(existing), normalized):
                return False
            raise EventConflictError("event identity is associated with different content")
        con.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (normalized["device_id"], normalized["seq"], normalized["event_id"],
             normalized["entity_id"], normalized["kind"], normalized["created_at"],
             json.dumps(normalized["payload"], separators=(",", ":"), sort_keys=True),
             normalized["parent_event_id"]),
        )
        return True


def vector_clock() -> dict[str, int]:
    """Return the largest contiguous sequence stored for every device."""
    with connect() as con:
        rows = con.execute("SELECT device_id, seq FROM events ORDER BY device_id, seq").fetchall()
    clocks: dict[str, int] = {}
    for row in rows:
        did, seq = row["device_id"], int(row["seq"])
        if seq == clocks.get(did, 0) + 1:
            clocks[did] = seq
    return clocks


def missing_events(peer_clock: dict[str, int]) -> list[dict[str, Any]]:
    return [event for event in all_events() if event["seq"] > int(peer_clock.get(event["device_id"], 0))]


def missing_events_page(
    peer_clock: dict[str, int], limit: int,
    after: tuple[str, int] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, int] | None]:
    events = missing_events(peer_clock)
    if after is not None:
        events = [event for event in events if (event["device_id"], event["seq"]) > after]
    page = events[:limit]
    cursor = (page[-1]["device_id"], page[-1]["seq"]) if len(events) > limit else None
    return page, cursor


def all_events(entity_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as con:
        if entity_id is None:
            rows = con.execute("SELECT * FROM events ORDER BY device_id, seq").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM events WHERE entity_id = ? ORDER BY created_at, device_id, seq", (entity_id,)
            ).fetchall()
    return [_event_from_row(row) for row in rows]


def store_blob(data: bytes, original_name: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    path = BLOB_DIR / digest
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise EventConflictError(f"local blob {digest} failed integrity validation")
    if not path.exists():
        path.write_bytes(data)
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?,?)", (digest, len(data), mime_type, original_name))
    return {"sha256": digest, "size_bytes": len(data), "mime_type": mime_type, "original_name": original_name}


def blob_path(sha256: str) -> Path:
    return BLOB_DIR / sha256


def known_blob_hashes() -> set[str]:
    with connect() as con:
        return {row["sha256"] for row in con.execute("SELECT sha256 FROM blobs")}


def blob_metadata(sha256: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
    return dict(row) if row else None


def register_blob_metadata(meta: dict[str, Any]) -> None:
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?,?)", (
            meta["sha256"], int(meta["size_bytes"]), meta.get("mime_type"), meta.get("original_name")
        ))


def _append_blob_chunk(sha256: str, data: bytes, start: int, total: int) -> dict[str, Any]:
    """Append a sequential resumable upload and atomically publish it when complete."""
    if total < 0 or start < 0 or start + len(data) > total:
        raise ValueError("invalid content range")
    final_path = blob_path(sha256)
    if final_path.is_file():
        if final_path.stat().st_size != total:
            raise ValueError("existing blob size does not match upload total")
        return {"received": final_path.stat().st_size, "total": total, "complete": True}
    partial = UPLOAD_DIR / f"{sha256}.part"
    current = partial.stat().st_size if partial.exists() else 0
    if current != start:
        raise ValueError(f"unexpected chunk start; server has {current} bytes")
    with partial.open("ab") as handle:
        handle.write(data)
    received = start + len(data)
    if received == total:
        digest = hashlib.sha256()
        with partial.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != sha256:
            partial.unlink(missing_ok=True)
            raise ValueError("completed upload failed SHA-256 validation")
        partial.replace(final_path)
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,NULL,NULL)", (sha256, total))
    return {"received": received, "total": total, "complete": received == total}


def append_blob_chunk(sha256: str, data: bytes, start: int, total: int) -> dict[str, Any]:
    with BLOB_UPLOAD_LOCK:
        return _append_blob_chunk(sha256, data, start, total)


def missing_blob_metadata() -> dict[str, dict[str, Any]]:
    missing: dict[str, dict[str, Any]] = {}
    for event in all_events():
        meta = event["payload"].get("blob")
        if meta and not blob_path(meta["sha256"]).is_file():
            missing[meta["sha256"]] = meta
    return missing


def entity_history(entity_id: str) -> dict[str, Any] | None:
    events = all_events(entity_id)
    if not events:
        return None
    parent_ids = {event["parent_event_id"] for event in events if event["parent_event_id"]}
    for event in events:
        merged = event["payload"].get("_merged_event_ids", [])
        if isinstance(merged, list):
            parent_ids.update(value for value in merged if isinstance(value, str))
    heads = [event for event in events if event["event_id"] not in parent_ids]
    heads.sort(key=lambda event: (event["created_at"], event["device_id"], event["seq"]))
    return {"entity_id": entity_id, "events": events, "heads": heads, "conflict": len(heads) > 1}


def current_entity(entity_id: str) -> dict[str, Any] | None:
    history = entity_history(entity_id)
    if history is None:
        return None
    live_heads = [head for head in history["heads"] if head["kind"] != "delete"]
    if not live_heads:
        return None
    # Never hide a live conflicting branch merely because a delete won the
    # deterministic ordering. A later conflict-resolution event can merge it.
    winner = live_heads[-1]
    public_payload = {key: value for key, value in winner["payload"].items() if not key.startswith("_")}
    return {
        "entity_id": entity_id, "kind": winner["kind"], "created_at": winner["created_at"],
        **public_payload, "_version": winner["event_id"], "_conflict": history["conflict"],
        "_branch_versions": [head["event_id"] for head in history["heads"]],
    }


def current_entities() -> list[dict[str, Any]]:
    entity_ids = dict.fromkeys(event["entity_id"] for event in all_events())
    return [item for entity_id in entity_ids if (item := current_entity(entity_id)) is not None]


def conflict_histories() -> list[dict[str, Any]]:
    entity_ids = dict.fromkeys(event["entity_id"] for event in all_events())
    histories = (entity_history(entity_id) for entity_id in entity_ids)
    return [history for history in histories if history and history["conflict"]]


def replace_derived_document(
    entity_id: str,
    source_event_id: str,
    extractor_id: str,
    text: str,
    metadata: dict[str, Any],
    chunks: list[dict[str, Any]],
    updated_at: str,
    status: str = "ready",
    error: str | None = None,
) -> None:
    """Atomically replace local derived data for one authoritative version."""
    with connect() as con:
        old_ids = [row["chunk_id"] for row in con.execute(
            "SELECT chunk_id FROM chunks WHERE entity_id = ?", (entity_id,)
        )]
        if old_ids:
            con.executemany("DELETE FROM embeddings WHERE chunk_id = ?", ((value,) for value in old_ids))
        con.execute("DELETE FROM chunks WHERE entity_id = ?", (entity_id,))
        con.execute("DELETE FROM derived_documents WHERE entity_id = ?", (entity_id,))
        con.execute(
            "INSERT INTO derived_documents VALUES(?,?,?,?,?,?,?,?)",
            (entity_id, source_event_id, extractor_id, text,
             json.dumps(metadata, separators=(",", ":"), sort_keys=True), status, error, updated_at),
        )
        con.executemany(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)",
            ((item["chunk_id"], entity_id, source_event_id, item["ordinal"], item["text"],
              item["start_char"], item["end_char"],
              json.dumps(item["locator"], separators=(",", ":"), sort_keys=True)) for item in chunks),
        )


def remove_stale_derived(current_versions: dict[str, str]) -> int:
    with connect() as con:
        rows = con.execute("SELECT entity_id, source_event_id FROM derived_documents").fetchall()
        stale = [row["entity_id"] for row in rows if current_versions.get(row["entity_id"]) != row["source_event_id"]]
        for entity_id in stale:
            chunk_ids = con.execute("SELECT chunk_id FROM chunks WHERE entity_id = ?", (entity_id,)).fetchall()
            con.executemany("DELETE FROM embeddings WHERE chunk_id = ?", ((row["chunk_id"],) for row in chunk_ids))
            con.execute("DELETE FROM chunks WHERE entity_id = ?", (entity_id,))
            con.execute("DELETE FROM derived_documents WHERE entity_id = ?", (entity_id,))
    return len(stale)


def derived_version(entity_id: str) -> str | None:
    with connect() as con:
        row = con.execute(
            "SELECT source_event_id FROM derived_documents WHERE entity_id = ? AND status = 'ready'", (entity_id,)
        ).fetchone()
    return row["source_event_id"] if row else None


def indexed_chunks() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """SELECT c.*, d.extractor_id, d.metadata_json
               FROM chunks c JOIN derived_documents d
                 ON d.entity_id = c.entity_id AND d.source_event_id = c.source_event_id
               WHERE d.status = 'ready' ORDER BY c.entity_id, c.ordinal"""
        ).fetchall()
    return [{
        "chunk_id": row["chunk_id"], "entity_id": row["entity_id"],
        "source_event_id": row["source_event_id"], "ordinal": int(row["ordinal"]),
        "text": row["text"], "start_char": int(row["start_char"]),
        "end_char": int(row["end_char"]), "locator": json.loads(row["locator_json"]),
        "extractor_id": row["extractor_id"], "metadata": json.loads(row["metadata_json"]),
    } for row in rows]


def replace_embeddings(model_id: str, vectors: list[tuple[str, int, bytes]]) -> None:
    with connect() as con:
        con.execute("DELETE FROM embeddings WHERE model_id = ?", (model_id,))
        con.executemany("INSERT INTO embeddings VALUES(?,?,?,?)", (
            (chunk_id, model_id, dimensions, vector) for chunk_id, dimensions, vector in vectors
        ))


def stored_embeddings(model_id: str) -> dict[str, tuple[int, bytes]]:
    with connect() as con:
        rows = con.execute(
            "SELECT chunk_id, dimensions, vector FROM embeddings WHERE model_id = ?", (model_id,)
        ).fetchall()
    return {row["chunk_id"]: (int(row["dimensions"]), row["vector"]) for row in rows}


def index_status() -> dict[str, Any]:
    with connect() as con:
        documents = con.execute("SELECT status, COUNT(*) AS n FROM derived_documents GROUP BY status").fetchall()
        chunks = con.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        embeddings = con.execute("SELECT model_id, COUNT(*) AS n FROM embeddings GROUP BY model_id").fetchall()
    return {
        "documents": {row["status"]: int(row["n"]) for row in documents},
        "chunks": int(chunks),
        "embeddings": {row["model_id"]: int(row["n"]) for row in embeddings},
    }


def save_pairing_offer(code_hash: str, expires_at: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM pairing_offers WHERE expires_at < ? OR used = 1", (int(time.time()),))
        con.execute("INSERT INTO pairing_offers(code_hash, expires_at, used) VALUES(?,?,0)",
                    (code_hash, expires_at))


def consume_pairing_offer(code_hash: str, now: int) -> bool:
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT expires_at, used FROM pairing_offers WHERE code_hash = ?", (code_hash,)
        ).fetchone()
        if row is None or row["used"] or int(row["expires_at"]) < now:
            con.rollback()
            return False
        con.execute("UPDATE pairing_offers SET used = 1 WHERE code_hash = ?", (code_hash,))
        con.commit()
        return True
    finally:
        con.close()


def upsert_peer(device_id: str, name: str, public_key: str, paired_at: str) -> None:
    with connect() as con:
        existing = con.execute("SELECT public_key FROM peers WHERE device_id = ?", (device_id,)).fetchone()
        if existing is not None and existing["public_key"] != public_key:
            raise EventConflictError("peer device_id is already paired with a different public key")
        con.execute(
            """INSERT INTO peers(device_id, name, public_key, paired_at, last_seen_at)
               VALUES(?,?,?,?,NULL)
               ON CONFLICT(device_id) DO UPDATE SET name = excluded.name""",
            (device_id, name, public_key, paired_at),
        )


def peer(device_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM peers WHERE device_id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def peers() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM peers ORDER BY name, device_id").fetchall()
    return [dict(row) for row in rows]


def forget_peer(device_id: str) -> bool:
    with connect() as con:
        result = con.execute("DELETE FROM peers WHERE device_id = ?", (device_id,))
    return result.rowcount > 0


def accept_nonce(device_id: str, nonce: str, seen_at: int) -> bool:
    with connect() as con:
        con.execute("DELETE FROM auth_nonces WHERE seen_at < ?", (seen_at - 600,))
        try:
            con.execute("INSERT INTO auth_nonces VALUES(?,?,?)", (device_id, nonce, seen_at))
            con.execute("UPDATE peers SET last_seen_at = ? WHERE device_id = ?",
                        (datetime_from_epoch(seen_at), device_id))
            return True
        except sqlite3.IntegrityError:
            return False


def datetime_from_epoch(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
