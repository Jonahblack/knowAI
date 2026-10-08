from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from . import auth, db

PROTOCOL_VERSION = "1.0"
MAX_EVENT_PAGE = 1_000
MAX_BLOB_CHUNK = 4 * 1024 * 1024
MAX_BLOB_BYTES = 1024 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")

router = APIRouter()


class PairingOfferIn(BaseModel):
    advertised_url: HttpUrl
    name: str = Field(default="Knowledge Node", min_length=1, max_length=100)


class PairingCompleteIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=40, max_length=100)
    code: str = Field(pattern=r"^\d{8}$")


class SyncRunIn(BaseModel):
    peer_url: HttpUrl


class PairingJoinIn(BaseModel):
    protocol_version: str
    device_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=40, max_length=100)
    url: HttpUrl
    code: str = Field(pattern=r"^\d{8}$")
    expires_at: datetime
    local_name: str = Field(default="Knowledge Node", min_length=1, max_length=100)


class ClockIn(BaseModel):
    clock: dict[str, int] = Field(default_factory=dict)
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=MAX_EVENT_PAGE)

    @field_validator("clock")
    @classmethod
    def validate_clock(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not device or sequence < 0 for device, sequence in value.items()):
            raise ValueError("clock entries require a device ID and non-negative sequence")
        return value


class SyncEvent(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=200)
    entity_id: str = Field(min_length=1, max_length=200)
    kind: Literal["note", "document", "upsert", "delete"]
    created_at: datetime
    payload: dict[str, Any]
    parent_event_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_event(self) -> "SyncEvent":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        blob = self.payload.get("blob")
        if blob is not None:
            if not isinstance(blob, dict) or not SHA256_PATTERN.fullmatch(str(blob.get("sha256", ""))):
                raise ValueError("invalid blob digest")
            if not isinstance(blob.get("size_bytes"), int) or blob["size_bytes"] < 0:
                raise ValueError("invalid blob size")
        return self

    def storage_dict(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["created_at"] = self.created_at.isoformat()
        return value


class EventBatch(BaseModel):
    events: list[SyncEvent] = Field(max_length=MAX_EVENT_PAGE)


class BlobQuery(BaseModel):
    hashes: list[str] = Field(max_length=MAX_EVENT_PAGE)

    @field_validator("hashes")
    @classmethod
    def validate_hashes(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not SHA256_PATTERN.fullmatch(item) for item in value):
            raise ValueError("hashes must be unique lowercase SHA-256 digests")
        return value


def _encode_cursor(value: tuple[str, int] | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps([value[0], value[1]], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str, int] | None:
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return str(decoded[0]), int(decoded[1])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid event cursor") from exc


@router.get("/protocol/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "minimum_protocol_version": PROTOCOL_VERSION,
        "device_id": db.device_id(),
        "public_key": auth.public_key_text(),
        "features": ["event-pagination", "bidirectional-events", "resumable-blobs", "ed25519-auth"],
        "limits": {"event_page": MAX_EVENT_PAGE, "blob_chunk_bytes": MAX_BLOB_CHUNK,
                   "blob_bytes": MAX_BLOB_BYTES},
    }


@router.post("/pairing/offers", dependencies=[Depends(auth.require_loopback)])
def create_pairing_offer(value: PairingOfferIn) -> dict[str, Any]:
    code, code_hash = auth.new_pairing_code()
    expires_at = int(time.time()) + 600
    db.save_pairing_offer(code_hash, expires_at)
    return {
        "protocol_version": PROTOCOL_VERSION, "device_id": db.device_id(), "name": value.name,
        "public_key": auth.public_key_text(), "url": str(value.advertised_url).rstrip("/"),
        "code": code, "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
    }


@router.post("/pairing/complete")
def complete_pairing(value: PairingCompleteIn) -> dict[str, Any]:
    try:
        auth.validate_public_key(value.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not db.consume_pairing_offer(auth.pairing_code_hash(value.code), int(time.time())):
        raise HTTPException(status_code=401, detail="pairing code is invalid, expired, or already used")
    try:
        db.upsert_peer(value.device_id, value.name, value.public_key, datetime.now(timezone.utc).isoformat())
    except db.EventConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "protocol_version": PROTOCOL_VERSION, "device_id": db.device_id(),
        "public_key": auth.public_key_text(), "paired": True,
    }


@router.post("/pairing/join", dependencies=[Depends(auth.require_loopback)])
async def join_pairing_offer(value: PairingJoinIn) -> dict[str, Any]:
    if value.protocol_version != PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail="pairing offer uses an unsupported protocol version")
    if value.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="pairing offer has expired")
    try:
        auth.validate_public_key(value.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = {
        "device_id": db.device_id(), "name": value.local_name,
        "public_key": auth.public_key_text(), "code": value.code,
    }
    base = str(value.url).rstrip("/")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        try:
            response = await client.post(f"{base}/pairing/complete", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"pairing peer error: {exc}") from exc
    result = response.json()
    if result.get("device_id") != value.device_id or result.get("public_key") != value.public_key:
        raise HTTPException(status_code=409, detail="pairing response does not match the scanned identity")
    try:
        db.upsert_peer(value.device_id, value.name, value.public_key, datetime.now(timezone.utc).isoformat())
    except db.EventConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"paired": True, "device_id": value.device_id, "peer_url": base}


@router.get("/pairing/peers", dependencies=[Depends(auth.require_loopback)])
def list_peers() -> list[dict[str, Any]]:
    return db.peers()


@router.delete("/pairing/peers/{device_id}", dependencies=[Depends(auth.require_loopback)])
def forget_peer(device_id: str) -> dict[str, bool]:
    return {"removed": db.forget_peer(device_id)}


@router.post("/sync/v1/run", dependencies=[Depends(auth.require_loopback)])
async def run_sync(value: SyncRunIn) -> dict[str, Any]:
    from .sync_client import synchronize
    try:
        return await synchronize(str(value.peer_url).rstrip("/"))
    except (ValueError, db.EventConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sync/v1/clock")
def clock(_: str = Depends(auth.require_peer)) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL_VERSION, "clock": db.vector_clock()}


@router.post("/sync/v1/events/query")
def query_events(value: ClockIn, _: str = Depends(auth.require_peer)) -> dict[str, Any]:
    page, next_cursor = db.missing_events_page(value.clock, value.limit, _decode_cursor(value.cursor))
    return {"events": page, "next_cursor": _encode_cursor(next_cursor)}


@router.post("/sync/v1/events")
def receive_events(value: EventBatch, _: str = Depends(auth.require_peer)) -> dict[str, int]:
    added = 0
    try:
        for event in value.events:
            added += int(db.ingest_event(event.storage_dict()))
    except db.EventConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"received": len(value.events), "added": added}


@router.get("/sync/v1/blobs/{sha256}")
def download_blob(sha256: str, _: str = Depends(auth.require_peer)):
    if not SHA256_PATTERN.fullmatch(sha256):
        raise HTTPException(status_code=400, detail="invalid SHA-256 digest")
    path = db.blob_path(sha256)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="blob not found")
    return FileResponse(path, headers={"ETag": f'"{sha256}"'})


@router.post("/sync/v1/missing-blobs")
def query_missing_blobs(value: BlobQuery, _: str = Depends(auth.require_peer)) -> dict[str, list[str]]:
    return {"missing": [digest for digest in value.hashes if not db.blob_path(digest).is_file()]}


@router.put("/sync/v1/blobs/{sha256}")
async def upload_blob_chunk(
    sha256: str,
    request: Request,
    content_range: str = Header(alias="Content-Range"),
    _: str = Depends(auth.require_peer),
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(sha256):
        raise HTTPException(status_code=400, detail="invalid SHA-256 digest")
    match = CONTENT_RANGE_PATTERN.fullmatch(content_range)
    empty_upload = content_range == "bytes */0"
    if match is None and not empty_upload:
        raise HTTPException(status_code=400, detail="Content-Range must be 'bytes start-end/total'")
    start, end, total = (0, -1, 0) if empty_upload else tuple(int(value) for value in match.groups())
    if total > MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="blob exceeds protocol limit")
    body = await request.body()
    if len(body) > MAX_BLOB_CHUNK:
        raise HTTPException(status_code=413, detail="blob chunk exceeds protocol limit")
    if (not empty_upload and end != start + len(body) - 1) or (empty_upload and body):
        raise HTTPException(status_code=400, detail="Content-Range does not match request body")
    try:
        return db.append_blob_chunk(sha256, body, start, total)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
