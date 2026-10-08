# knowAI Android

Native, offline-first Android client for Knowledge Sync Protocol v1.

## Current Phase 4B slice

- Room-backed append-only event log with atomic local sequences and gap-safe clocks
- App-private, SHA-256-addressed blob storage
- Ed25519 device identity protected by an Android Keystore wrapping key
- Signed, client-driven event and blob synchronization
- WorkManager opportunistic sync (connected network required)
- Compose note list/editor and visible conflict branches
- Android share target for text, links, documents, and photos
- Photo picker/camera ingestion entry points
- Local-only AI interfaces for embeddings and chat runtimes

The AI interfaces deliberately do not make generated data authoritative or
replicated. A concrete LiteRT-LM model package is the next device-specific
integration step; model selection, download, memory limits, and licensing need
to be decided on actual target hardware.

## Open in Android Studio

Install the latest stable Android Studio with Android SDK 37 and JDK 17, then
open this `android/` directory as a project. Android Studio can install the
declared Gradle and SDK components during project sync.

This repository does not currently include a generated Gradle wrapper because
the development machine used for this phase had no JDK, Gradle, Android Studio,
or Android SDK installed. After the first successful sync, generate and commit
it with:

```powershell
gradle wrapper --gradle-version 9.6.0
```

Then verify:

```powershell
.\gradlew.bat test
.\gradlew.bat assembleDebug
```

## Pair and sync

1. Start the server on the LAN and create a pairing offer as described in
   `../server/README.md`.
2. In the Android app, paste the complete pairing-offer JSON (QR scanning is a
   UI adapter still to be added) and pair.
3. Use **Sync now**, or leave background sync enabled. WorkManager retries
   transient failures with backoff.

Cleartext HTTP is allowed only for LAN development. Do not expose either node
to the public internet until Phase 5 encrypted transport is complete.

## Protocol fixtures

The unit-test resource set points directly at `../protocol/fixtures`; Android
does not maintain a divergent copy of the wire examples.
