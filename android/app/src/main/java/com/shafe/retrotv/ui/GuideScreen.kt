package com.shafe.retrotv.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.shafe.retrotv.ads.AdBanner
import com.shafe.retrotv.ads.AdsController
import com.shafe.retrotv.data.Channel
import com.shafe.retrotv.data.Epg
import com.shafe.retrotv.data.nowNext
import kotlinx.coroutines.delay

/**
 * Full-screen channel list with now/next. Click or OK tunes; long-press (or tap the star) toggles
 * a favourite. Works with touch and D-pad: every row is focusable and shows a focus ring.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun GuideScreen(
    channels: List<Channel>,
    currentChannel: Channel?,
    epg: Epg,
    favorites: Set<String>,
    ads: AdsController,
    onTune: (Channel) -> Unit,
    onToggleFavorite: (Channel) -> Unit,
) {
    var favoritesOnly by remember { mutableStateOf(false) }
    val shown = remember(channels, favorites, favoritesOnly) {
        if (favoritesOnly) channels.filter { it.name in favorites } else channels
    }
    val now by produceState(System.currentTimeMillis()) {
        while (true) {
            delay(30_000)
            value = System.currentTimeMillis()
        }
    }
    val listState = rememberLazyListState()
    val focusCurrent = remember { FocusRequester() }

    LaunchedEffect(Unit) {
        val idx = shown.indexOfFirst { it.number == currentChannel?.number }.coerceAtLeast(0)
        listState.scrollToItem((idx - 2).coerceAtLeast(0))
        runCatching { focusCurrent.requestFocus() }
    }

    Column(Modifier.fillMaxSize().background(Color(0xF2080820)).statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("CHANNEL GUIDE", color = Accent, fontFamily = PixelFont, fontSize = 14.sp, modifier = Modifier.weight(1f))
            FilterChip(selected = !favoritesOnly, onClick = { favoritesOnly = false }, label = { Text("All") })
            Spacer(Modifier.width(8.dp))
            FilterChip(selected = favoritesOnly, onClick = { favoritesOnly = true }, label = { Text("Favorites") })
        }
        if (shown.isEmpty()) {
            Text(
                if (favoritesOnly) "No favorites yet. Long-press a channel to add one." else "No channels loaded.",
                color = Color.White,
                modifier = Modifier.padding(16.dp),
            )
        }
        LazyColumn(state = listState, modifier = Modifier.weight(1f).fillMaxWidth()) {
            itemsIndexed(shown, key = { _, c -> c.number }) { _, ch ->
                val isCurrent = ch.number == currentChannel?.number
                GuideRow(
                    channel = ch,
                    isCurrent = isCurrent,
                    isFavorite = ch.name in favorites,
                    nowNext = epg.nowNext(ch.epgId, now),
                    now = now,
                    onClick = { onTune(ch) },
                    onToggleFavorite = { onToggleFavorite(ch) },
                    modifier = if (isCurrent) Modifier.focusRequester(focusCurrent) else Modifier,
                )
            }
        }
        if (ads.enabled && ads.ready) AdBanner()
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun GuideRow(
    channel: Channel,
    isCurrent: Boolean,
    isFavorite: Boolean,
    nowNext: com.shafe.retrotv.data.NowNext,
    now: Long,
    onClick: () -> Unit,
    onToggleFavorite: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    val border = when {
        focused -> Modifier.border(3.dp, Accent)
        isCurrent -> Modifier.border(1.dp, Accent.copy(alpha = 0.5f))
        else -> Modifier
    }
    Row(
        modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .then(border)
            .background(if (focused) Color(0xFF1C1C50) else Panel)
            .combinedClickable(interactionSource = interaction, indication = null, onClick = onClick, onLongClick = onToggleFavorite)
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(channel.number.toString().padStart(2, '0'), color = Accent, fontFamily = PixelFont, fontSize = 12.sp, modifier = Modifier.width(44.dp))
        Column(Modifier.weight(1f)) {
            Text(channel.name, color = Color.White, fontFamily = PixelFont, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(6.dp))
            val prog = nowNext.now
            val line = prog?.displayTitle
                ?: channel.description.ifBlank { "No schedule available" }
            Text(line, color = Color(0xFFCCCCDD), fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (prog != null) {
                Spacer(Modifier.height(6.dp))
                LinearProgressIndicator(progress = { prog.progress(now) }, modifier = Modifier.fillMaxWidth().height(3.dp), color = Accent)
            }
        }
        // Star works as a touch target on phones; D-pad users long-press the row instead.
        Box(
            Modifier
                .padding(start = 8.dp)
                .combinedClickable(onClick = onToggleFavorite)
                .padding(8.dp),
        ) {
            Text(if (isFavorite) "★" else "☆", color = if (isFavorite) Accent else Color.Gray, fontSize = 20.sp)
        }
    }
}
