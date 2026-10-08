package com.knowai.node.crypto

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import java.security.KeyStore
import java.security.SecureRandom
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class DeviceIdentity(context: Context) {
    private val preferences = context.getSharedPreferences("device_identity", Context.MODE_PRIVATE)
    private val keyAlias = "knowai-identity-wrapper-v1"

    val deviceId: String by lazy {
        preferences.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            preferences.edit().putString("device_id", it).commit()
        }
    }

    private val seed: ByteArray by lazy { loadOrCreateSeed() }

    val publicKey: String by lazy {
        encode(Ed25519PrivateKeyParameters(seed).generatePublicKey().encoded)
    }

    fun sign(message: ByteArray): String {
        val signer = Ed25519Signer()
        signer.init(true, Ed25519PrivateKeyParameters(seed))
        signer.update(message, 0, message.size)
        return encode(signer.generateSignature())
    }

    private fun loadOrCreateSeed(): ByteArray {
        val encrypted = preferences.getString("seed", null)
        val iv = preferences.getString("seed_iv", null)
        if (encrypted != null && iv != null) {
            return cipher(Cipher.DECRYPT_MODE, decode(iv)).doFinal(decode(encrypted))
        }
        val raw = ByteArray(Ed25519PrivateKeyParameters.KEY_SIZE).also(SecureRandom()::nextBytes)
        val encryptor = cipher(Cipher.ENCRYPT_MODE)
        val ciphertext = encryptor.doFinal(raw)
        check(preferences.edit()
            .putString("seed", encode(ciphertext))
            .putString("seed_iv", encode(encryptor.iv))
            .commit())
        return raw
    }

    private fun cipher(mode: Int, iv: ByteArray? = null): Cipher =
        Cipher.getInstance("AES/GCM/NoPadding").apply {
            if (mode == Cipher.ENCRYPT_MODE) init(mode, wrappingKey())
            else init(mode, wrappingKey(), GCMParameterSpec(128, requireNotNull(iv)))
        }

    private fun wrappingKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(keyAlias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    keyAlias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build(),
            )
            generateKey()
        }
    }

    companion object {
        fun encode(bytes: ByteArray): String = Base64.encodeToString(
            bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING,
        )

        fun decode(text: String): ByteArray = Base64.decode(text, Base64.URL_SAFE or Base64.NO_WRAP)
    }
}
