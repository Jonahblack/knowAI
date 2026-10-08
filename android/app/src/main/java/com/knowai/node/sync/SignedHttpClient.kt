package com.knowai.node.sync

import com.knowai.node.crypto.DeviceIdentity
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

class SignedHttpClient(
    private val identity: DeviceIdentity,
    private val client: OkHttpClient,
) {
    fun execute(
        baseUrl: String,
        method: String,
        path: String,
        body: ByteArray = byteArrayOf(),
        headers: Map<String, String> = emptyMap(),
    ): Response {
        val timestamp = Instant.now().epochSecond.toString()
        val nonce = UUID.randomUUID().toString()
        val digest = sha256(body)
        val canonical = canonical(method, path, timestamp, nonce, digest)
        val requestBody = if (method == "GET") null else body.toRequestBody(
            headers["Content-Type"]?.toMediaType() ?: "application/json".toMediaType(),
        )
        val builder = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .method(method, requestBody)
            .header("X-Knowledge-Device", identity.deviceId)
            .header("X-Knowledge-Timestamp", timestamp)
            .header("X-Knowledge-Nonce", nonce)
            .header("X-Knowledge-Content-SHA256", digest)
            .header("X-Knowledge-Signature", identity.sign(canonical))
        headers.forEach(builder::header)
        return client.newCall(builder.build()).execute()
    }

    companion object {
        fun sha256(body: ByteArray): String = MessageDigest.getInstance("SHA-256")
            .digest(body).joinToString("") { "%02x".format(it) }

        fun canonical(
            method: String,
            path: String,
            timestamp: String,
            nonce: String,
            digest: String,
        ): ByteArray = "${method.uppercase()}\n$path\n$timestamp\n$nonce\n$digest".toByteArray(Charsets.UTF_8)
    }
}
