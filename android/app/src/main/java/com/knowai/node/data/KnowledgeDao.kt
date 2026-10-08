package com.knowai.node.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface KnowledgeDao {
    @Query("SELECT * FROM events ORDER BY deviceId, seq")
    suspend fun allEvents(): List<EventEntity>

    @Query("SELECT * FROM events ORDER BY createdAt DESC, eventId DESC")
    fun observeEvents(): Flow<List<EventEntity>>

    @Query("SELECT * FROM events WHERE entityId = :entityId ORDER BY createdAt, eventId")
    suspend fun entityEvents(entityId: String): List<EventEntity>

    @Query("SELECT * FROM events WHERE eventId = :eventId LIMIT 1")
    suspend fun event(eventId: String): EventEntity?

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertEvent(event: EventEntity): Long

    @Query("SELECT * FROM device_state WHERE deviceId = :deviceId")
    suspend fun state(deviceId: String): DeviceStateEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putState(state: DeviceStateEntity)

    @Query("SELECT * FROM peers ORDER BY name")
    fun observePeers(): Flow<List<PeerEntity>>

    @Query("SELECT * FROM peers ORDER BY name")
    suspend fun peers(): List<PeerEntity>

    @Query("SELECT * FROM peers WHERE deviceId = :deviceId LIMIT 1")
    suspend fun peer(deviceId: String): PeerEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putPeer(peer: PeerEntity)

    @Query("DELETE FROM peers WHERE deviceId = :deviceId")
    suspend fun deletePeer(deviceId: String)
}
