# Knowledge Sync Protocol v1.0

This directory is the implementation-neutral contract shared by the Python
server and native clients. SQLite files are never exchanged.

## Principles

- Every device has an Ed25519 identity and stable `device_id`.
- Pairing is explicit, one-time, and initiated from the node's loopback UI/API.
- Replicated events and original blobs are authoritative.
- Extraction, chunks, captions, embeddings, and model output never replicate.
- A sync initiator both pushes and pulls; a mobile device never needs to host a
  background HTTP server.
- Event delivery is paginated and idempotent.
- Blob uploads are sequential, resumable, and verified by SHA-256.

## Pairing

1. On the server device, create `POST /pairing/offers` from loopback.
2. Encode the returned object as a QR code.
3. The joining device verifies the scanned server identity and sends its own
   identity plus the eight-digit code to `POST /pairing/complete`.
4. The joining device stores the server public key from the scanned offer; the
   server stores the joining device public key.

The Python node provides loopback-only `POST /pairing/join` to perform steps 3
and 4 for another desktop node. Android will perform those steps after scanning
the QR offer.

Offers expire after ten minutes and can be consumed once.

## Signed requests

Authenticated endpoints require these headers:

- `X-Knowledge-Device`
- `X-Knowledge-Timestamp` — Unix seconds, within five minutes.
- `X-Knowledge-Nonce` — a new UUID for every request.
- `X-Knowledge-Content-SHA256` — lowercase digest of the exact HTTP body.
- `X-Knowledge-Signature` — unpadded URL-safe base64 Ed25519 signature.

The signed bytes are UTF-8:

```text
METHOD\n/path-without-query\nTIMESTAMP\nNONCE\nCONTENT_SHA256
```

Query parameters are intentionally excluded; protocol v1 authenticated
operations carry pagination state in JSON bodies.

## Client-driven sync session

1. Fetch authenticated `GET /sync/v1/clock` from the peer.
2. Query the local store using that clock and push pages to
   `POST /sync/v1/events`.
3. Send the local clock to `POST /sync/v1/events/query`, following
   `next_cursor` until it is null, and ingest every returned page.
4. Reconcile blob references from the merged event set. Send locally available
   hashes to `POST /sync/v1/missing-blobs` to learn which blobs the peer needs.
5. Download missing blobs with `GET /sync/v1/blobs/{sha256}`. HTTP range
   requests may be used when supported by the server response stack.
6. Upload missing blobs in chunks with `PUT /sync/v1/blobs/{sha256}` and
   `Content-Range: bytes start-end/total`.
   Empty blobs use `Content-Range: bytes */0` with an empty body.
7. Repeat event exchange until both clocks are unchanged. Concurrent writes
   during a session are therefore handled without a global lock.

The Python node exposes loopback-only `POST /sync/v1/run` as a convenience
orchestrator for desktop nodes. Android will implement these same steps in its
own sync worker rather than asking a server to connect back to the phone.

Limits and optional features are returned by `GET /protocol/capabilities`.

## Compatibility

Breaking changes require a new major protocol version and endpoint prefix.
Additive response fields are allowed. Clients must ignore unknown response
fields and must not send event kinds they do not understand.
