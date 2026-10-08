package com.knowai.node.data

import android.content.Context
import java.io.File
import java.io.InputStream
import java.security.MessageDigest

class BlobStore(context: Context) {
    private val root = File(context.filesDir, "blobs").apply { mkdirs() }

    fun file(hash: String): File {
        require(hash.matches(Regex("[0-9a-f]{64}")))
        return File(root, hash)
    }

    fun contains(hash: String, size: Long? = null): Boolean =
        file(hash).let { it.isFile && (size == null || it.length() == size) && sha256(it.inputStream()) == hash }

    fun import(input: InputStream): StoredBlob {
        val temporary = File.createTempFile("incoming-", ".part", root)
        return try {
            input.use { source -> temporary.outputStream().use(source::copyTo) }
            val hash = sha256(temporary.inputStream())
            val destination = file(hash)
            if (!destination.exists() && !temporary.renameTo(destination)) {
                temporary.copyTo(destination, overwrite = false)
            }
            StoredBlob(hash, destination.length())
        } finally {
            temporary.delete()
        }
    }

    fun commitDownload(hash: String, bytes: ByteArray) {
        require(bytes.size.toLong() >= 0)
        require(sha256(bytes.inputStream()) == hash) { "downloaded blob digest mismatch" }
        val destination = file(hash)
        val temporary = File.createTempFile("download-", ".part", root)
        try {
            temporary.writeBytes(bytes)
            if (!temporary.renameTo(destination)) temporary.copyTo(destination, overwrite = true)
        } finally {
            temporary.delete()
        }
    }

    private fun sha256(input: InputStream): String {
        val digest = MessageDigest.getInstance("SHA-256")
        input.use { source ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = source.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}

data class StoredBlob(val sha256: String, val sizeBytes: Long)
