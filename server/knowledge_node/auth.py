from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException, Request

from . import db

MAX_SIGNED_BODY_BYTES = 16 * 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 300


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def private_key() -> Ed25519PrivateKey:
    db.ensure_dirs()
    key_file = db.ROOT / "identity_ed25519.key"
    if key_file.exists():
        return Ed25519PrivateKey.from_private_bytes(key_file.read_bytes())
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    try:
        with key_file.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        return Ed25519PrivateKey.from_private_bytes(key_file.read_bytes())
    return key


def public_key_text() -> str:
    raw = private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(raw)


def canonical_request(method: str, path: str, timestamp: str, nonce: str, content_sha256: str) -> bytes:
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{content_sha256}".encode("utf-8")


def signed_headers(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()
    signature = private_key().sign(canonical_request(method, path, timestamp, nonce, digest))
    return {
        "X-Knowledge-Device": db.device_id(),
        "X-Knowledge-Timestamp": timestamp,
        "X-Knowledge-Nonce": nonce,
        "X-Knowledge-Content-SHA256": digest,
        "X-Knowledge-Signature": _b64(signature),
    }


def new_pairing_code() -> tuple[str, str]:
    code = f"{secrets.randbelow(100_000_000):08d}"
    digest = hmac.new(
        private_key().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        ), code.encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return code, digest


def pairing_code_hash(code: str) -> str:
    return hmac.new(
        private_key().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        ), code.encode("ascii"), hashlib.sha256,
    ).hexdigest()


def validate_public_key(value: str) -> None:
    try:
        raw = _unb64(value)
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise ValueError("invalid Ed25519 public key") from exc
    if len(raw) != 32:
        raise ValueError("invalid Ed25519 public key length")


async def require_peer(request: Request) -> str:
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    if content_length > MAX_SIGNED_BODY_BYTES:
        raise HTTPException(status_code=413, detail="signed request body is too large")
    device_id = request.headers.get("X-Knowledge-Device", "")
    timestamp = request.headers.get("X-Knowledge-Timestamp", "")
    nonce = request.headers.get("X-Knowledge-Nonce", "")
    claimed_digest = request.headers.get("X-Knowledge-Content-SHA256", "")
    signature_text = request.headers.get("X-Knowledge-Signature", "")
    peer = db.peer(device_id)
    if peer is None:
        raise HTTPException(status_code=401, detail="unknown peer")
    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid authentication timestamp") from exc
    now = int(time.time())
    if abs(now - timestamp_value) > MAX_CLOCK_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="authentication timestamp is outside the allowed window")
    try:
        uuid.UUID(nonce)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid authentication nonce") from exc
    body = await request.body()
    actual_digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_digest, claimed_digest):
        raise HTTPException(status_code=401, detail="request content digest mismatch")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_unb64(peer["public_key"]))
        public_key.verify(
            _unb64(signature_text),
            canonical_request(request.method, request.url.path, timestamp, nonce, claimed_digest),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid request signature") from exc
    if not db.accept_nonce(device_id, nonce, now):
        raise HTTPException(status_code=409, detail="authentication nonce was already used")
    return device_id


def require_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="this administrative operation is loopback-only")
