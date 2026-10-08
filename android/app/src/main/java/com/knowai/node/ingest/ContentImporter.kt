package com.knowai.node.ingest

import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.provider.OpenableColumns
import com.google.gson.JsonObject
import com.knowai.node.data.BlobStore
import com.knowai.node.data.EventRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

class ContentImporter(
    private val context: Context,
    private val blobs: BlobStore,
    private val repository: EventRepository,
) {
    suspend fun importUri(uri: Uri, suppliedType: String? = null) = withContext(Dispatchers.IO) {
        val resolver = context.contentResolver
        val stored = resolver.openInputStream(uri)?.let(blobs::import)
            ?: error("shared content cannot be opened")
        val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) it.getString(0) else null
        } ?: "shared-${stored.sha256.take(12)}"
        repository.addDocument(documentPayload(stored.sha256, stored.sizeBytes, name, suppliedType ?: resolver.getType(uri)))
    }

    suspend fun importBitmap(bitmap: Bitmap) = withContext(Dispatchers.IO) {
        val bytes = ByteArrayOutputStream().use { output ->
            check(bitmap.compress(Bitmap.CompressFormat.JPEG, 92, output))
            output.toByteArray()
        }
        val stored = blobs.import(ByteArrayInputStream(bytes))
        repository.addDocument(documentPayload(
            stored.sha256, stored.sizeBytes, "camera-${System.currentTimeMillis()}.jpg", "image/jpeg",
        ))
    }

    private fun documentPayload(hash: String, size: Long, filename: String, contentType: String?) =
        JsonObject().apply {
            addProperty("title", filename)
            add("tags", com.google.gson.JsonArray())
            add("blob", JsonObject().apply {
                addProperty("sha256", hash)
                addProperty("size_bytes", size)
                addProperty("filename", filename)
                addProperty("content_type", contentType ?: "application/octet-stream")
            })
        }
}
