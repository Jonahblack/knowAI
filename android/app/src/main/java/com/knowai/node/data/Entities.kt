package com.knowai.node.data

import androidx.room.Entity
import androidx.room.Index

@Entity(
    tableName = "events",
    primaryKeys = ["deviceId", "seq"],
    indices = [Index(value = ["eventId"], unique = true), Index("entityId")],
)
data class EventEntity(
    val deviceId: String,
    val seq: Long,
    val eventId: String,
    val entityId: String,
    val kind: String,
    val createdAt: String,
    val payloadJson: String,
    val parentEventId: String?,
)

@Entity(tableName = "device_state")
data class DeviceStateEntity(
    @androidx.room.PrimaryKey val deviceId: String,
    val nextSeq: Long,
)

@Entity(tableName = "peers")
data class PeerEntity(
    @androidx.room.PrimaryKey val deviceId: String,
    val name: String,
    val publicKey: String,
    val baseUrl: String,
    val pairedAt: String,
)
