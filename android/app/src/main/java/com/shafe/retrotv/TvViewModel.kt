package com.shafe.retrotv

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.shafe.retrotv.data.Channel
import com.shafe.retrotv.data.ChannelRepository
import com.shafe.retrotv.data.Epg
import com.shafe.retrotv.data.EpgRepository
import com.shafe.retrotv.data.Prefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class TvViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = Prefs(app)

    var channels by mutableStateOf<List<Channel>>(emptyList())
        private set
    var loading by mutableStateOf(true)
        private set
    var epg by mutableStateOf<Epg>(emptyMap())
        private set
    var currentIndex by mutableStateOf(0)
        private set
    var favorites by mutableStateOf(prefs.favorites)
        private set

    val current: Channel? get() = channels.getOrNull(currentIndex)

    init {
        viewModelScope.launch {
            val list = withContext(Dispatchers.IO) { ChannelRepository.load(app) }
            channels = list
            currentIndex = list.indexOfFirst { it.name == prefs.lastChannelName }.coerceAtLeast(0)
            loading = false

            val ids = list.mapNotNull { it.epgId }.toSet()
            while (true) {
                val fresh = withContext(Dispatchers.IO) { EpgRepository.load(ids) }
                if (fresh.isNotEmpty()) epg = fresh
                delay(EPG_REFRESH_MS)
            }
        }
    }

    fun select(index: Int) {
        if (channels.isEmpty()) return
        currentIndex = ((index % channels.size) + channels.size) % channels.size
        prefs.lastChannelName = channels[currentIndex].name
    }

    fun next() = select(currentIndex + 1)
    fun previous() = select(currentIndex - 1)

    fun selectByNumber(number: Int): Boolean {
        val i = channels.indexOfFirst { it.number == number }
        if (i < 0) return false
        select(i)
        return true
    }

    fun toggleFavorite(channel: Channel) {
        favorites = if (channel.name in favorites) favorites - channel.name else favorites + channel.name
        prefs.favorites = favorites
    }

    private companion object {
        const val EPG_REFRESH_MS = 6 * 3600_000L
    }
}
