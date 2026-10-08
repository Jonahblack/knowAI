package com.knowai.node

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.knowai.node.model.KnowledgeEvent
import com.knowai.node.sync.SignedHttpClient
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import org.junit.Assert.assertEquals
import org.junit.Test
import java.util.Base64

class ProtocolConformanceTest {
    private val gson = Gson()

    @Test
    fun authFixtureMatchesCanonicalDigestAndSignature() {
        val fixture = resource("auth-signature.json").reader().use {
            gson.fromJson(it, JsonObject::class.java)
        }
        val body = fixture["body"].asString.toByteArray()
        val digest = SignedHttpClient.sha256(body)
        assertEquals(fixture["content_sha256"].asString, digest)
        val canonical = SignedHttpClient.canonical(
            fixture["method"].asString,
            fixture["path"].asString,
            fixture["timestamp"].asString,
            fixture["nonce"].asString,
            digest,
        )
        assertEquals(resource("auth-canonical.txt").readText().trim(), canonical.decodeToString())

        val privateKey = Ed25519PrivateKeyParameters(decode(fixture["private_key_seed"].asString))
        val signer = Ed25519Signer().apply {
            init(true, privateKey)
            update(canonical, 0, canonical.size)
        }
        assertEquals(fixture["signature"].asString, encode(signer.generateSignature()))
        assertEquals(fixture["public_key"].asString, encode(privateKey.generatePublicKey().encoded))
    }

    @Test
    fun outOfOrderFixtureOnlyAdvancesAcrossContiguousSequence() {
        val fixture = resource("events.json").reader().use { gson.fromJson(it, JsonObject::class.java) }
        val events = fixture.getAsJsonArray("events").map {
            gson.fromJson(it, KnowledgeEvent::class.java)
        }.associateBy { it.seq }
        val received = mutableSetOf<Long>()
        val clocks = fixture.getAsJsonArray("delivery_order").map { order ->
            received += events.getValue(order.asLong).seq
            var contiguous = 0L
            while (contiguous + 1 in received) contiguous++
            if (contiguous == 0L) emptyMap() else mapOf("device-a" to contiguous)
        }
        assertEquals(emptyMap<String, Long>(), clocks[0])
        assertEquals(mapOf("device-a" to 2L), clocks[1])
    }

    private fun resource(name: String) = requireNotNull(javaClass.classLoader!!.getResource(name))
    private fun decode(value: String): ByteArray = Base64.getUrlDecoder().decode(value)
    private fun encode(value: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(value)
}
