from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from . import auth, db

EVENT_PAGE_SIZE = 500
BLOB_CHUNK_SIZE = 4 * 1024 * 1024


def _json_body(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


async def _request(
    client: httpx.AsyncClient, method: str, base: str, path: str,
    body: bytes = b"", extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    headers = auth.signed_headers(method, path, body)
    if body:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    response = await client.request(method, f"{base}{path}", content=body, headers=headers)
    response.raise_for_status()
    return response


def _blob_hashes(events: list[dict[str, Any]]) -> set[str]:
    return {
        blob["sha256"] for event in events
        if isinstance((blob := event.get("payload", {}).get("blob")), dict)
    }


async def _upload_blobs(
    client: httpx.AsyncClient, base: str, digests: set[str],
) -> int:
    available = [digest for digest in sorted(digests) if db.blob_path(digest).is_file()]
    if not available:
        return 0
    missing: list[str] = []
    for offset in range(0, len(available), 1_000):
        body = _json_body({"hashes": available[offset:offset + 1_000]})
        response = await _request(client, "POST", base, "/sync/v1/missing-blobs", body)
        missing.extend(response.json()["missing"])
    completed = 0
    for digest in missing:
        path = db.blob_path(digest)
        total = path.stat().st_size
        if total == 0:
            upload_path = f"/sync/v1/blobs/{digest}"
            await _request(
                client, "PUT", base, upload_path, b"",
                {"Content-Type": "application/octet-stream", "Content-Range": "bytes */0"},
            )
            completed += 1
            continue
        start = 0
        with path.open("rb") as handle:
            while chunk := handle.read(BLOB_CHUNK_SIZE):
                end = start + len(chunk) - 1
                upload_path = f"/sync/v1/blobs/{digest}"
                response = await _request(
                    client, "PUT", base, upload_path, chunk,
                    {"Content-Type": "application/octet-stream",
                     "Content-Range": f"bytes {start}-{end}/{total}"},
                )
                start = int(response.json()["received"])
        completed += 1
    return completed


async def _download_missing_blobs(
    client: httpx.AsyncClient, base: str, candidate_digests: set[str],
) -> tuple[int, list[str]]:
    downloaded = 0
    unavailable: list[str] = []
    missing = db.missing_blob_metadata()
    for digest in sorted(candidate_digests & missing.keys()):
        meta = missing[digest]
        path = f"/sync/v1/blobs/{digest}"
        try:
            response = await _request(client, "GET", base, path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                unavailable.append(digest)
                continue
            raise
        data = response.content
        if len(data) != int(meta["size_bytes"]):
            raise ValueError(f"blob size mismatch for {digest}")
        stored = db.store_blob(data, meta.get("original_name"), meta.get("mime_type"))
        if stored["sha256"] != digest:
            raise ValueError(f"blob hash mismatch for {digest}")
        downloaded += 1
    return downloaded, unavailable


async def synchronize(peer_url: str) -> dict[str, Any]:
    base = peer_url.rstrip("/")
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        capabilities_response = await client.get(f"{base}/protocol/capabilities")
        capabilities_response.raise_for_status()
        capabilities = capabilities_response.json()
        if capabilities.get("protocol_version") != "1.0":
            raise ValueError("peer does not support protocol 1.0")
        paired = db.peer(str(capabilities.get("device_id", "")))
        if paired is None or paired["public_key"] != capabilities.get("public_key"):
            raise ValueError("peer URL does not match a paired device identity")

        remote_clock_response = await _request(client, "GET", base, "/sync/v1/clock")
        remote_clock = remote_clock_response.json()["clock"]
        pushed = 0
        pushed_blob_hashes: set[str] = set()
        cursor: tuple[str, int] | None = None
        while True:
            page, cursor = db.missing_events_page(remote_clock, EVENT_PAGE_SIZE, cursor)
            if not page:
                break
            body = _json_body({"events": page})
            result = await _request(client, "POST", base, "/sync/v1/events", body)
            pushed += int(result.json()["added"])
            pushed_blob_hashes.update(_blob_hashes(page))
            if cursor is None:
                break
        blobs_uploaded = await _upload_blobs(client, base, pushed_blob_hashes)

        pulled = 0
        pulled_blob_hashes: set[str] = set()
        cursor_text: str | None = None
        while True:
            body = _json_body({"clock": db.vector_clock(), "cursor": cursor_text, "limit": EVENT_PAGE_SIZE})
            response = await _request(client, "POST", base, "/sync/v1/events/query", body)
            page = response.json()
            for event in page["events"]:
                from .sync_api import SyncEvent
                validated = SyncEvent.model_validate(event).storage_dict()
                pulled += int(db.ingest_event(validated))
                pulled_blob_hashes.update(_blob_hashes([validated]))
            cursor_text = page["next_cursor"]
            if cursor_text is None:
                break
        retryable_blob_hashes = pulled_blob_hashes | set(db.missing_blob_metadata())
        blobs_downloaded, unavailable_blobs = await _download_missing_blobs(
            client, base, retryable_blob_hashes
        )
    return {
        "peer_device_id": capabilities["device_id"], "events_pushed": pushed,
        "events_pulled": pulled, "blobs_uploaded": blobs_uploaded,
        "blobs_downloaded": blobs_downloaded, "unavailable_blobs": unavailable_blobs,
        "local_clock": db.vector_clock(),
    }
