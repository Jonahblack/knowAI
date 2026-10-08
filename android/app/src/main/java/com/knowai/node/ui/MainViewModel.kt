package com.knowai.node.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.knowai.node.AppGraph
import com.knowai.node.data.PeerEntity
import com.knowai.node.model.MaterializedEntity
import com.knowai.node.model.PairingOffer
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

data class UiStatus(val message: String? = null, val busy: Boolean = false)

class MainViewModel(private val graph: AppGraph) : ViewModel() {
    val entities: StateFlow<List<MaterializedEntity>> = graph.repository.observeEntities()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    val peers: StateFlow<List<PeerEntity>> = graph.database.dao().observePeers()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    val status = MutableStateFlow(UiStatus())

    fun addNote(title: String, text: String) = launch("Note saved") {
        require(title.isNotBlank() || text.isNotBlank()) { "note is empty" }
        graph.repository.createNote(title.ifBlank { "Untitled" }, text)
    }

    fun pair(offerJson: String, localName: String) = launch("Peer paired") {
        val offer = graph.gson.fromJson(offerJson, PairingOffer::class.java)
        val peer = graph.peerApi.pair(offer, localName.ifBlank { "Android" })
        graph.database.dao().putPeer(peer)
    }

    fun sync() = launch("Sync complete") {
        require(peers.value.isNotEmpty()) { "pair a peer first" }
        val summaries = graph.database.dao().peers().map { graph.sync.synchronize(it) }
        val pushed = summaries.sumOf { it.pushedEvents }
        val pulled = summaries.sumOf { it.pulledEvents }
        status.value = UiStatus("Sync complete: $pushed pushed, $pulled pulled")
    }

    fun clearStatus() { status.value = UiStatus() }

    private fun launch(success: String, block: suspend () -> Unit) {
        viewModelScope.launch {
            status.value = UiStatus(busy = true)
            runCatching { block() }
                .onSuccess { if (status.value.busy) status.value = UiStatus(success) }
                .onFailure { status.value = UiStatus(it.message ?: "Operation failed") }
        }
    }

    class Factory(private val graph: AppGraph) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T = MainViewModel(graph) as T
    }
}
