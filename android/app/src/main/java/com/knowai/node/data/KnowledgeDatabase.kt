package com.knowai.node.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [EventEntity::class, DeviceStateEntity::class, PeerEntity::class],
    version = 1,
    exportSchema = true,
)
abstract class KnowledgeDatabase : RoomDatabase() {
    abstract fun dao(): KnowledgeDao

    companion object {
        fun create(context: Context): KnowledgeDatabase = Room.databaseBuilder(
            context.applicationContext,
            KnowledgeDatabase::class.java,
            "knowledge.db",
        ).build()
    }
}
