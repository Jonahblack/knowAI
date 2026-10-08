package com.knowai.node.sync

import com.google.gson.Gson
import com.knowai.node.crypto.DeviceIdentity
import com.knowai.node.data.PeerEntity
import com.knowai.node.model.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.time.Instant

class PeerApi(
    private val gson: Gson,
    private val identity: DeviceIdentity,
    private val rawClient: OkHttpClient,
    private val signed: SignedHttpClient,
) {
    suspend fun pair(offer: PairingOffer, localName: String): PeerEntity = withContext(Dispatchers.IO) {
        require(offer.protocolVersion == PROTOCOL_VERSION) { "unsupported protocol version" }
        require(Instant.parse(offer.expiresAt).isAfter(Instant.now())) { "pairing offer expired" }
        require(offer.code.matches(Regex("\\d{8}"))) { "invalid pairing code" }
        require(DeviceIdentity.decode(offer.publicKey).size == 32) { "invalid server public key" }
        val body = gson.toJson(
            PairingComplete(identity.deviceId, localName, identity.publicKey, offer.code),
        ).toByteArray()
        val request = Request.Builder()
            .url(offer.url.trimEnd('/') + "/pairing/complete")
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()
        val result = rawClient.newCall(request).execute().use { response ->
            response.requireSuccess()
            gson.fromJson(response.body!!.charStream(), PairingResult::class.java)
        }
        require(result.paired && result.deviceId == offer.deviceId && result.publicKey == offer.publicKey) {
            "pairing response does not match scanned identity"
        }
        PeerEntity(offer.deviceId, offer.name, offer.publicKey, offer.url.trimEnd('/'), Instant.now().toString())
    }

    suspend fun clock(peer: PeerEntity): Map<String, Long> = signedJson(
        peer, "GET", "/sync/v1/clock", null, ClockResponse::class.java,
    ).also { require(it.protocolVersion == PROTOCOL_VERSION) }.clock

    suspend fun pushEvents(peer: PeerEntity, events: List<KnowledgeEvent>): ReceiveResult = signedJson(
        peer, "POST", "/sync/v1/events", EventBatch(events), ReceiveResult::class.java,
    )

    suspend fun queryEvents(peer: PeerEntity, query: ClockQuery): EventPage = signedJson(
        peer, "POST", "/sync/v1/events/query", query, EventPage::class.java,
    )

    suspend fun missingBlobs(peer: PeerEntity, hashes: List<String>): List<String> = signedJson(
        peer, "POST", "/sync/v1/missing-blobs", BlobQuery(hashes), MissingBlobs::class.java,
    ).missing

    suspend fun downloadBlob(peer: PeerEntity, hash: String): ByteArray = withContext(Dispatchers.IO) {
        signed.execute(peer.baseUrl, "GET", "/sync/v1/blobs/$hash").use { response ->
            response.requireSuccess()
            response.body!!.bytes()
        }
    }

    suspend fun uploadChunk(
        peer: PeerEntity,
        hash: String,
        bytes: ByteArray,
        start: Long,
        total: Long,
    ) = withContext(Dispatchers.IO) {
        val range = if (total == 0L) "bytes */0" else "bytes $start-${start + bytes.size - 1}/$total"
        signed.execute(
            peer.baseUrl, "PUT", "/sync/v1/blobs/$hash", bytes,
            mapOf("Content-Type" to "application/octet-stream", "Content-Range" to range),
        ).use { it.requireSuccess() }
    }

    private suspend fun <T> signedJson(
        peer: PeerEntity,
        method: String,
        path: String,
        value: Any?,
        type: Class<T>,
    ): T = withContext(Dispatchers.IO) {
        val bytes = value?.let { gson.toJson(it) }?.toByteArray() ?: byteArrayOf()
        signed.execute(peer.baseUrl, method, path, bytes).use { response ->
            response.requireSuccess()
            gson.fromJson(response.body!!.charStream(), type)
        }
    }
}

private fun okhttp3.Response.requireSuccess() {
    if (!isSuccessful) throw SyncHttpException(code, body?.string().orEmpty())
}

class SyncHttpException(val status: Int, detail: String) :
    IllegalStateException("peer returned HTTP $status: $detail")
