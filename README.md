# knowAI

A local-first personal knowledge system with independent server and Android
applications joined by a versioned replication protocol.

## Repository layout

- `server/` — Python/FastAPI desktop and Raspberry Pi node.
- `protocol/` — implementation-neutral schemas, examples, and conformance data.
- `android/` — native Kotlin/Compose Android knowledge node (Phase 4B in progress).

The applications do not share database files or runtime code. They implement
the same event, authentication, and synchronization contract from `protocol/`.

See `server/README.md` for the currently runnable application and
`protocol/README.md` for the mobile-ready sync contract.
