# Local Knowledge Node v0.5

A deliberately small prototype of a **local-first replicated personal knowledge store**.

Each device owns a complete local database. Devices can work offline and exchange new events when they can reach one another. Files are stored by SHA-256 content hash. AI/vector indexes are *derived locally* and are intentionally not synchronized.

## Architecture

- **Canonical replicated data**: SQLite append-only event log
- **Files**: content-addressed blob store (`SHA-256`)
- **Sync state**: gap-safe vector clock (`device_id -> highest contiguous sequence`)
- **Derived retrieval**: versioned extraction, chunks, embeddings, and local vector storage
- **Search**: lexical, semantic, or reciprocal-rank hybrid retrieval with citations
- **Local assistant**: model-independent tools and an OpenAI-compatible Gemma adapter
- **Peer security**: Ed25519 identities, QR pairing, signed requests, and replay protection
- **Later**: native Android client and encrypted roaming transport

## Why append-only events?

A phone and desktop can both change while offline. Instead of syncing an SQLite file (unsafe) or guessing which copy is newer, every device creates uniquely identified events with a monotonically increasing local sequence number. Sync is a union of events.

Updates and deletes name the event version on which they are based. Concurrent
offline edits therefore remain as sibling branch heads instead of silently
overwriting one another. Normal entity reads select a deterministic live branch
for convenience and include `_conflict`, `_version`, and `_branch_versions`.
Use the history and conflict endpoints to inspect every branch.

## Run on a desktop / Raspberry Pi

Python 3.11+ recommended.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn knowledge_node.app:app --host 0.0.0.0 --port 8765
```

For local embeddings and image captions, install the optional AI dependencies.
Model weights are downloaded by their libraries the first time they are used:

```bash
pip install -r requirements-ai.txt
```

Open `http://127.0.0.1:8765/docs` for the interactive API.

Data defaults to `~/.local_knowledge`. Override it:

```bash
# Windows PowerShell
$env:KNOWLEDGE_HOME="D:\\KnowledgeNode"

# Linux/macOS
export KNOWLEDGE_HOME="$HOME/KnowledgeNode"
```

## Add a note

```bash
curl -X POST http://127.0.0.1:8765/notes \
  -H "Content-Type: application/json" \
  -d '{"title":"Paint","text":"Downstairs bathroom idea: green","tags":["home"]}'
```

## Add a file

```bash
curl -X POST http://127.0.0.1:8765/files \
  -F "file=@manual.pdf" \
  -F "tags=manual,appliance"
```

## Update or delete a note

Read the entity first and use its `_version` as `base_event_id`:

```bash
curl http://127.0.0.1:8765/entities/ENTITY_ID

curl -X PUT http://127.0.0.1:8765/notes/ENTITY_ID \
  -H "Content-Type: application/json" \
  -d '{"base_event_id":"EVENT_ID","title":"Paint","text":"Use forest green","tags":["home"]}'

curl -X DELETE "http://127.0.0.1:8765/entities/ENTITY_ID?base_event_id=EVENT_ID"
```

An older `base_event_id` is accepted deliberately: it creates a visible branch
when another device has already edited the same version. Inspect branches with:

```bash
curl http://127.0.0.1:8765/conflicts
curl http://127.0.0.1:8765/entities/ENTITY_ID/history
```

Resolve a conflict by submitting every current head and the chosen merged text:

```bash
curl -X POST http://127.0.0.1:8765/notes/ENTITY_ID/resolve \
  -H "Content-Type: application/json" \
  -d '{"base_event_ids":["LEFT_EVENT","RIGHT_EVENT"],"title":"Paint","text":"Merged decision","tags":["home"]}'
```

## Sync two nodes

Sync protocol v1 is client-driven: the initiating device pushes events the peer
is missing and then pulls its own missing pages. This lets Android synchronize
without hosting a background web server. Generate a ten-minute pairing offer
from loopback:

```bash
curl -X POST http://127.0.0.1:8765/pairing/offers \
  -H "Content-Type: application/json" \
  -d '{"advertised_url":"http://192.168.1.10:8765","name":"Home node"}'
```

Encode that response as a QR code. The joining device stores the advertised
identity and completes pairing with its own device ID and Ed25519 public key.
Every subsequent sync request is signed and replay-protected.

For another Python node, send the scanned/transported offer object to its
loopback-only join endpoint, adding `local_name` if desired:

```bash
curl -X POST http://127.0.0.1:8765/pairing/join \
  -H "Content-Type: application/json" \
  -d @pairing-offer.json
```

Initiate a bidirectional session from that same local node:

```bash
curl -X POST http://127.0.0.1:8765/sync/v1/run \
  -H "Content-Type: application/json" \
  -d '{"peer_url":"http://192.168.1.10:8765"}'
```

The complete exchange, signing format, pagination rules, blob upload contract,
schemas, and cross-platform fixtures live in [`../protocol/`](../protocol/).

**Do not expose this v0.5 API directly to the public internet. Signed HTTP
authenticates peers and content but does not encrypt metadata or payloads.**

## Build the local retrieval index

Extraction, chunks, captions, and embeddings are local derived data. They are
not synchronized. Build the lightweight lexical index after adding or syncing
content:

```bash
curl -X POST http://127.0.0.1:8765/index/rebuild \
  -H "Content-Type: application/json" \
  -d '{}'
```

Enable local embeddings and image captions when the optional AI dependencies
are installed:

```bash
curl -X POST http://127.0.0.1:8765/index/rebuild \
  -H "Content-Type: application/json" \
  -d '{"embed":true,"caption_images":true,"force":true}'
```

`force` is useful after changing an extractor or caption model. Normal rebuilds
only process new entity versions. Check progress and failures with
`GET /index/status`.

Search modes are `lexical`, `semantic`, and `hybrid`:

```bash
curl "http://127.0.0.1:8765/search?q=reset+the+boiler&mode=hybrid&limit=5"
```

Every result includes a citation containing the authoritative entity and event
IDs, filename/title, page or paragraph locator when available, and character
range. Phase 3 can pass these results to a model without losing provenance.

## Connect a local Gemma model

The node talks to any local server implementing the OpenAI-compatible
`POST /v1/chat/completions` interface with tool calls. LM Studio is the default
example because its local server exposes this interface directly. Start and
load a Gemma model in your chosen runtime, then configure the node before
starting Uvicorn:

```powershell
$env:KNOWLEDGE_LLM_BASE_URL="http://127.0.0.1:1234/v1"
$env:KNOWLEDGE_LLM_MODEL="your-loaded-gemma-model-id"
uvicorn knowledge_node.app:app --host 127.0.0.1 --port 8765
```

Ask a grounded question:

```bash
curl -X POST http://127.0.0.1:8765/assistant/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"How do I reset the boiler?"}'
```

Assistant requests are read-only by default. Explicitly enable `add_note` and
`attach_file` for a request only when mutations are intended:

```bash
curl -X POST http://127.0.0.1:8765/assistant/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Remember that the filter was replaced today.","allow_writes":true}'
```

The response includes the answer, source records, valid and invalid citation
IDs, and an auditable tool trace. Retrieved text is labeled as untrusted
evidence in the model prompt. The application validates calls against a fixed
allowlist and never dynamically executes a model-provided function name.

The tool schemas are available at `GET /tools`. Each tool can also be invoked
directly at:

- `POST /tools/search_knowledge`
- `POST /tools/get_item`
- `POST /tools/add_note`
- `POST /tools/attach_file`

`attach_file` accepts user-supplied base64 content, is limited to 10 MiB, and
can verify a caller-provided SHA-256 digest. Use `POST /files` for larger files.

## Roadmap

### Phase 1 — Local knowledge core (complete)
- [x] Per-device SQLite store
- [x] Stable device identity
- [x] Append-only event log
- [x] Notes
- [x] File/blob ingestion
- [x] SHA-256 integrity
- [x] LAN pull synchronization
- [x] Version-aware update/delete endpoints
- [x] Conflict/branch inspection API
- [x] Explicit branch resolution
- [x] Atomic local sequence allocation
- [x] Gap-safe vector clocks and idempotent event ingestion
- [x] Retryable missing-blob reconciliation
- [x] Replication validation and core regression tests

### Phase 2 — Useful retrieval (complete)
- [x] Text extraction from PDF, DOCX, and plain-text documents
- [x] Photo metadata and optional local captions
- [x] Stable, overlapping chunks with source locators
- [x] Optional local embedding model
- [x] Rebuildable local SQLite vector index
- [x] Hybrid lexical + semantic search
- [x] Citation-ready retrieval results for AI responses

Derived content records its source event plus extractor, chunker, caption, and
embedding model identifiers. A lightweight lexical-only deployment does not
require a model runtime.

### Phase 3 — Gemma integration (complete)
- [x] Define tools: `search_knowledge`, `get_item`, `add_note`, `attach_file`
- [x] Connect to a desktop Gemma server over localhost
- [x] Prompt assembly with retrieved evidence and citation auditing
- [x] Multi-turn, allowlisted tool execution with read-only defaults
- [x] Model-independent OpenAI-compatible adapter

### Phase 4A — Mobile-ready protocol (complete)
- [x] Separate server, protocol, and Android application boundaries
- [x] Version and capability negotiation
- [x] Client-driven bidirectional event exchange
- [x] Paginated event delivery
- [x] Resumable, SHA-256-verified blob uploads
- [x] Per-device Ed25519 identity
- [x] Expiring one-time QR pairing contract
- [x] Signed requests with timestamp and replay protection
- [x] Cross-platform schemas and conformance fixtures

### Phase 4B — Android
- [x] Native Android storage using the same logical schema
- [x] Background/opportunistic peer sync
- [x] Lightweight local embedding baseline and replaceable runtime boundary
- [ ] Production semantic embedding model via LiteRT
- [ ] Gemma via LiteRT-LM / supported Android runtime
- [x] Share-sheet action: "Save to Knowledge"
- [x] Camera/photo ingestion
- [ ] QR scanner UI for pairing offers (manual JSON pairing works)
- [ ] Validate build and behavior on target Android hardware

### Phase 5 — Secure roaming sync
- [x] Per-device public/private identity
- [x] Pairing with QR code
- [x] Mutual request authentication
- [ ] Encrypted transport
- [ ] Peer discovery on LAN
- [ ] Optional private overlay network (e.g. Tailscale) for away-from-home sync
- [ ] Sync policy controls: Wi-Fi only, charging only, blobs on demand, etc.

### Phase 6 — Personal knowledge features
- [ ] Receipts/warranties
- [ ] Recipes
- [ ] Household inventory
- [ ] Family journal
- [ ] Photo semantic search
- [ ] Calendar/import connectors
- [ ] Backup/export/restore

## Important design rule

Do **not** synchronize vector databases or generated summaries as authoritative state. Sync original knowledge + metadata. Embeddings, OCR, captions, thumbnails, and indexes should be rebuildable caches, tagged by the model/version that produced them.

## Tests

Install the development dependencies and run the isolated core suite:

```bash
pip install -r requirements-dev.txt
pytest
```

The same command can be run from the repository root; the root `pytest.ini`
selects the server suite and adds the server package to Python's import path.

The suite covers protocol fixtures, request signatures and replay rejection,
pagination, resumable blob upload, out-of-order event delivery, conflicting event
identities, concurrent local sequence allocation, branch detection, deletion,
missing-blob reconciliation, stable chunking, stale-version invalidation,
lexical citations, rank fusion, and stored-vector retrieval.
The agent tests additionally cover the tool allowlist, immediate indexing after
writes, attachment integrity, multi-turn tool execution, citation auditing, and
rejection of write tools when they are not enabled.

## Current trust boundary

This remains a LAN-oriented prototype. Sync peers are paired and authenticated,
but HTTP transport is not encrypted. Keep the service off the public internet
or place it behind a private encrypted overlay until TLS or equivalent transport
protection is implemented. Pairing administration is restricted to loopback.
