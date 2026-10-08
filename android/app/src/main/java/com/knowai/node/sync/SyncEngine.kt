package com.knowai.node.sync

import com.knowai.node.data.BlobStore
import com.knowai.node.data.EventRepository
import com.knowai.node.data.PeerEntity
import com.knowai.node.model.ClockQuery
import com.knowai.node.model.MAX_BLOB_CHUNK
import com.knowai.node.model.MAX_EVENT_PAGE

class SyncEngine(
    private val repository: EventRepository,
    private val blobs: BlobStore,
    private val api: PeerApi,
) {
    suspend fun synchronize(peer: PeerEntity): SyncSummary {
        var pushed = 0
        var pulled = 0
        repeat(MAX_SETTLE_ROUNDS) {
            val before = repository.clock()
            val remoteClock = api.clock(peer)
            repository.missingEvents(remoteClock).chunked(MAX_EVENT_PAGE).forEach { page ->
                pushed += api.pushEvents(peer, page).added
            }

            var cursor: String? = null
            do {
                val page = api.queryEvents(peer, ClockQuery(repository.clock(), cursor, MAX_EVENT_PAGE))
                pulled += repository.ingest(page.events)
                cursor = page.nextCursor
            } while (cursor != null)

            reconcileBlobs(peer)
            if (before == repository.clock() && remoteClock == api.clock(peer)) {
                return SyncSummary(pushed, pulled, true)
            }
        }
        return SyncSummary(pushed, pulled, false)
    }

    private suspend fun reconcileBlobs(peer: PeerEntity) {
        val references = repository.referencedBlobs()
        for ((hash, size) in references) {
            if (!blobs.contains(hash, size)) {
                try {
                    blobs.commitDownload(hash, api.downloadBlob(peer, hash))
                } catch (error: SyncHttpException) {
                    if (error.status != 404) throw error
                }
            }
        }

        val available = references.filter { (hash, size) -> blobs.contains(hash, size) }.keys.toList()
        api.missingBlobs(peer, available).forEach { hash ->
            val file = blobs.file(hash)
            if (file.length() == 0L) {
                api.uploadChunk(peer, hash, byteArrayOf(), 0, 0)
            } else {
                file.inputStream().use { input ->
                    var offset = 0L
                    val buffer = ByteArray(MAX_BLOB_CHUNK)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        api.uploadChunk(peer, hash, buffer.copyOf(count), offset, file.length())
                        offset += count
                    }
                }
            }
        }
    }

    companion object { private const val MAX_SETTLE_ROUNDS = 3 }
}

data class SyncSummary(val pushedEvents: Int, val pulledEvents: Int, val settled: Boolean)
