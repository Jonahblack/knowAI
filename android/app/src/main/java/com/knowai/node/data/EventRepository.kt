package com.knowai.node.data

import androidx.room.withTransaction
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.knowai.node.model.KnowledgeEvent
import com.knowai.node.model.MaterializedEntity
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import java.time.Instant
import java.util.UUID

class EventCollision(message: String) : IllegalStateException(message)

class EventRepository(
    private val database: KnowledgeDatabase,
    private val deviceId: String,
    private val gson: Gson,
) {
    private val dao = database.dao()

    fun observeEntities(): Flow<List<MaterializedEntity>> = dao.observeEvents().map(::materialize)

    suspend fun createNote(title: String, text: String, tags: List<String> = emptyList()): KnowledgeEvent =
        appendLocal(
            entityId = UUID.randomUUID().toString(),
            kind = "note",
            payload = JsonObject().apply {
                addProperty("title", title)
                addProperty("text", text)
                add("tags", gson.toJsonTree(tags))
            },
        )

    suspend fun updateNote(
        entityId: String,
        baseEventId: String,
        title: String,
        text: String,
        tags: List<String> = emptyList(),
    ): KnowledgeEvent = appendLocal(
        entityId = entityId,
        kind = "note",
        parentEventId = baseEventId,
        payload = JsonObject().apply {
            addProperty("title", title)
            addProperty("text", text)
            add("tags", gson.toJsonTree(tags))
        },
    )

    suspend fun addDocument(payload: JsonObject): KnowledgeEvent = appendLocal(
        entityId = UUID.randomUUID().toString(),
        kind = "document",
        payload = payload,
    )

    suspend fun appendLocal(
        entityId: String,
        kind: String,
        payload: JsonObject,
        parentEventId: String? = null,
    ): KnowledgeEvent = database.withTransaction {
        val state = dao.state(deviceId) ?: DeviceStateEntity(deviceId, 1)
        val event = KnowledgeEvent(
            deviceId = deviceId,
            seq = state.nextSeq,
            eventId = UUID.randomUUID().toString(),
            entityId = entityId,
            kind = kind,
            createdAt = Instant.now().toString(),
            payload = payload,
            parentEventId = parentEventId,
        )
        check(dao.insertEvent(event.toEntity(gson)) != -1L)
        dao.putState(state.copy(nextSeq = state.nextSeq + 1))
        event
    }

    suspend fun ingest(events: List<KnowledgeEvent>): Int = database.withTransaction {
        var added = 0
        for (event in events) {
            require(event.seq > 0 && event.deviceId.isNotBlank() && event.eventId.isNotBlank())
            val existing = dao.event(event.eventId)
            if (existing != null) {
                if (existing.toModel(gson) != event) {
                    throw EventCollision("event ID has different content: ${event.eventId}")
                }
                continue
            }
            if (dao.insertEvent(event.toEntity(gson)) == -1L) {
                throw EventCollision("device sequence has different content: ${event.deviceId}/${event.seq}")
            }
            added++
        }
        added
    }

    suspend fun clock(): Map<String, Long> {
        val grouped = dao.allEvents().groupBy { it.deviceId }
        return grouped.mapNotNull { (origin, events) ->
            var contiguous = 0L
            for (seq in events.map { it.seq }.sorted()) {
                if (seq == contiguous + 1) contiguous = seq
                else if (seq > contiguous + 1) break
            }
            if (contiguous == 0L) null else origin to contiguous
        }.toMap()
    }

    suspend fun missingEvents(remoteClock: Map<String, Long>): List<KnowledgeEvent> =
        dao.allEvents()
            .filter { it.seq > (remoteClock[it.deviceId] ?: 0L) }
            .map { it.toModel(gson) }

    suspend fun allEvents(): List<KnowledgeEvent> = dao.allEvents().map { it.toModel(gson) }

    suspend fun referencedBlobs(): Map<String, Long> = buildMap {
        allEvents().forEach { event ->
            val blob = event.payload.getAsJsonObject("blob") ?: return@forEach
            val hash = blob.get("sha256")?.asString ?: return@forEach
            val size = blob.get("size_bytes")?.asLong ?: return@forEach
            put(hash, size)
        }
    }

    private fun materialize(rows: List<EventEntity>): List<MaterializedEntity> = rows
        .groupBy { it.entityId }
        .mapNotNull { (entityId, entityRows) ->
            val events = entityRows.map { it.toModel(gson) }
            val parents = buildSet {
                events.mapNotNullTo(this) { it.parentEventId }
                events.forEach { event ->
                    event.payload.getAsJsonArray("_merged_event_ids")
                        ?.filter { it.isJsonPrimitive && it.asJsonPrimitive.isString }
                        ?.mapTo(this) { it.asString }
                }
            }
            val heads = events.filter { it.eventId !in parents }
                .sortedWith(compareBy({ it.createdAt }, { it.deviceId }, { it.seq }))
            val liveHeads = heads.filter { it.kind != "delete" }
            if (liveHeads.isEmpty()) null else MaterializedEntity(
                entityId = entityId,
                selected = liveHeads.last(),
                branchHeads = heads,
            )
        }
        .sortedByDescending { it.selected.createdAt }
}

fun KnowledgeEvent.toEntity(gson: Gson) = EventEntity(
    deviceId, seq, eventId, entityId, kind, createdAt, gson.toJson(payload), parentEventId,
)

fun EventEntity.toModel(gson: Gson) = KnowledgeEvent(
    deviceId, seq, eventId, entityId, kind, createdAt,
    gson.fromJson(payloadJson, JsonObject::class.java), parentEventId,
)
