package com.knowai.node.share

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import androidx.core.content.IntentCompat
import com.knowai.node.MainActivity
import com.knowai.node.appGraph
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class ShareReceiverActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (intent.action != Intent.ACTION_SEND) return finish()
        CoroutineScope(Dispatchers.Main).launch {
            runCatching {
                val uri = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, android.net.Uri::class.java)
                if (uri != null) {
                    appGraph.importer.importUri(uri, intent.type)
                } else {
                    val text = intent.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
                    require(text.isNotBlank()) { "nothing was shared" }
                    appGraph.repository.createNote(
                        intent.getStringExtra(Intent.EXTRA_SUBJECT) ?: "Shared to knowAI",
                        text,
                    )
                }
            }
            withContext(Dispatchers.Main) {
                startActivity(Intent(this@ShareReceiverActivity, MainActivity::class.java))
                finish()
            }
        }
    }
}
