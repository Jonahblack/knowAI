package com.knowai.node

import android.app.Application
import com.google.gson.Gson
import com.knowai.node.crypto.DeviceIdentity
import com.knowai.node.data.BlobStore
import com.knowai.node.data.EventRepository
import com.knowai.node.data.KnowledgeDatabase
import com.knowai.node.ingest.ContentImporter
import com.knowai.node.sync.PeerApi
import com.knowai.node.sync.SignedHttpClient
import com.knowai.node.sync.SyncEngine
import com.knowai.node.sync.SyncScheduler
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

class KnowledgeApplication : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)
        SyncScheduler.schedule(this)
    }
}

class AppGraph(application: Application) {
    val gson = Gson()
    val identity = DeviceIdentity(application)
    val database = KnowledgeDatabase.create(application)
    val repository = EventRepository(database, identity.deviceId, gson)
    val blobs = BlobStore(application)
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .followRedirects(false)
        .build()
    val peerApi = PeerApi(gson, identity, http, SignedHttpClient(identity, http))
    val sync = SyncEngine(repository, blobs, peerApi)
    val importer = ContentImporter(application, blobs, repository)
}

val android.content.Context.appGraph: AppGraph
    get() = (applicationContext as KnowledgeApplication).graph
