package com.knowai.node.model

import com.google.gson.JsonObject
import com.google.gson.annotations.SerializedName

const val PROTOCOL_VERSION = "1.0"
const val MAX_EVENT_PAGE = 1_000
const val MAX_BLOB_CHUNK = 4 * 1024 * 1024

data class KnowledgeEvent(
    @SerializedName("device_id") val deviceId: String,
    val seq: Long,
    @SerializedName("event_id") val eventId: String,
    @SerializedName("entity_id") val entityId: String,
    val kind: String,
    @SerializedName("created_at") val createdAt: String,
    val payload: JsonObject,
    @SerializedName("parent_event_id") val parentEventId: String?,
)

data class ClockResponse(
    @SerializedName("protocol_version") val protocolVersion: String,
    val clock: Map<String, Long>,
)

data class ClockQuery(
    val clock: Map<String, Long>,
    val cursor: String? = null,
    val limit: Int = 500,
)

data class EventPage(
    val events: List<KnowledgeEvent>,
    @SerializedName("next_cursor") val nextCursor: String?,
)

data class EventBatch(val events: List<KnowledgeEvent>)
data class ReceiveResult(val received: Int, val added: Int)
data class BlobQuery(val hashes: List<String>)
data class MissingBlobs(val missing: List<String>)

data class PairingOffer(
    @SerializedName("protocol_version") val protocolVersion: String,
    @SerializedName("device_id") val deviceId: String,
    val name: String,
    @SerializedName("public_key") val publicKey: String,
    val url: String,
    val code: String,
    @SerializedName("expires_at") val expiresAt: String,
)

data class PairingComplete(
    @SerializedName("device_id") val deviceId: String,
    val name: String,
    @SerializedName("public_key") val publicKey: String,
    val code: String,
)

data class PairingResult(
    @SerializedName("protocol_version") val protocolVersion: String,
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("public_key") val publicKey: String,
    val paired: Boolean,
)

data class BlobDescriptor(
    val sha256: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    val filename: String? = null,
    @SerializedName("content_type") val contentType: String? = null,
)

data class MaterializedEntity(
    val entityId: String,
    val selected: KnowledgeEvent,
    val branchHeads: List<KnowledgeEvent>,
) {
    val hasConflict: Boolean get() = branchHeads.size > 1
}
