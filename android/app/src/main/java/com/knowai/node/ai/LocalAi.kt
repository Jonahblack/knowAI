package com.knowai.node.ai

import kotlin.math.sqrt

/** Generated vectors and answers are local caches and never enter sync events. */
interface EmbeddingEngine {
    val modelId: String
    suspend fun embed(text: String): FloatArray
}

/** Lightweight offline baseline; replace with a packaged LiteRT embedding model. */
class HashEmbeddingEngine(private val dimensions: Int = 384) : EmbeddingEngine {
    override val modelId = "hash-embedding-v1-$dimensions"

    override suspend fun embed(text: String): FloatArray {
        val vector = FloatArray(dimensions)
        text.lowercase().split(Regex("[^\\p{L}\\p{N}]+"))
            .filter(String::isNotBlank)
            .forEach { token ->
                val hash = token.hashCode()
                val index = (hash and Int.MAX_VALUE) % dimensions
                vector[index] += if (hash and 1 == 0) 1f else -1f
            }
        val norm = sqrt(vector.sumOf { (it * it).toDouble() }).toFloat()
        if (norm > 0f) for (index in vector.indices) vector[index] /= norm
        return vector
    }
}

interface LocalChatRuntime {
    val modelId: String
    suspend fun generate(prompt: String): String
}

class ModelNotInstalledRuntime : LocalChatRuntime {
    override val modelId = "not-installed"
    override suspend fun generate(prompt: String): String =
        error("Install and select a supported LiteRT-LM model before using local chat")
}
