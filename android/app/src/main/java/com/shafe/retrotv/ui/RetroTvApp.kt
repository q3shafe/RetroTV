package com.shafe.retrotv.ui

import android.view.KeyEvent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.shafe.retrotv.TvViewModel
import com.shafe.retrotv.ads.AdsController
import com.shafe.retrotv.data.Channel
import com.shafe.retrotv.data.NowNext
import com.shafe.retrotv.data.nowNext
import kotlinx.coroutines.delay
import java.text.DateFormat
import java.util.Date
import kotlin.math.abs

private const val OVERLAY_MS = 4_000L
private const val NUMBER_ENTRY_MS = 1_500L
private const val SWIPE_THRESHOLD_PX = 140f

@Composable
fun RetroTvApp(viewModel: TvViewModel, ads: AdsController, isTv: Boolean) {
    val channels = viewModel.channels
    val current = viewModel.current

    var showGuide by remember { mutableStateOf(false) }
    var overlayVisible by remember { mutableStateOf(true) }
    var overlayToken by remember { mutableStateOf(0) }
    var numberBuffer by remember { mutableStateOf("") }
    val rootFocus = remember { FocusRequester() }

    fun flashOverlay() {
        overlayVisible = true
        overlayToken++
    }

    fun userZap(action: () -> Unit) {
        action()
        ads.onChannelChanged()
        flashOverlay()
    }

    // Hide the info bar a few seconds after the last interaction or channel change.
    LaunchedEffect(overlayToken, current?.number) {
        overlayVisible = true
        delay(OVERLAY_MS)
        overlayVisible = false
    }

    // Number entry: commit a moment after the last digit.
    LaunchedEffect(numberBuffer) {
        if (numberBuffer.isEmpty()) return@LaunchedEffect
        delay(NUMBER_ENTRY_MS)
        numberBuffer.toIntOrNull()?.let { n -> userZap { viewModel.selectByNumber(n) } }
        numberBuffer = ""
    }

    // Keep key focus on the root whenever the guide is closed so D-pad zapping works.
    LaunchedEffect(showGuide, channels.isNotEmpty()) {
        if (!showGuide) runCatching { rootFocus.requestFocus() }
    }

    BackHandler(enabled = showGuide) { showGuide = false }

    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black)
            .focusRequester(rootFocus)
            .focusable()
            .onPreviewKeyEvent { e ->
                if (showGuide || e.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                val code = e.nativeKeyEvent.keyCode
                when (code) {
                    KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_CHANNEL_UP -> { userZap { viewModel.next() }; true }
                    KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_CHANNEL_DOWN -> { userZap { viewModel.previous() }; true }
                    KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER,
                    KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_GUIDE -> { showGuide = true; true }
                    KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT -> { flashOverlay(); true }
                    in KeyEvent.KEYCODE_0..KeyEvent.KEYCODE_9 -> {
                        numberBuffer = (numberBuffer + (code - KeyEvent.KEYCODE_0)).takeLast(3); flashOverlay(); true
                    }
                    in KeyEvent.KEYCODE_NUMPAD_0..KeyEvent.KEYCODE_NUMPAD_9 -> {
                        numberBuffer = (numberBuffer + (code - KeyEvent.KEYCODE_NUMPAD_0)).takeLast(3); flashOverlay(); true
                    }
                    else -> false
                }
            }
            // Phone gestures: tap toggles the info bar, vertical swipe changes channel.
            .pointerInput(Unit) {
                detectTapGestures(onTap = { overlayVisible = !overlayVisible; if (overlayVisible) overlayToken++ })
            }
            .pointerInput(Unit) {
                var total = 0f
                detectVerticalDragGestures(
                    onDragStart = { total = 0f },
                    onDragEnd = {
                        if (abs(total) > SWIPE_THRESHOLD_PX) {
                            // Swipe up = next channel, like flicking a channel wheel.
                            userZap { if (total < 0) viewModel.next() else viewModel.previous() }
                        }
                    },
                ) { _, dy -> total += dy }
            },
    ) {
        when {
            viewModel.loading -> CenterMessage("LOADING CHANNELS...")
            current == null -> CenterMessage("NO CHANNELS FOUND")
            else -> VideoSurface(url = current.url)
        }

        if (current != null && overlayVisible && !showGuide) {
            InfoBar(
                channel = current,
                nowNext = viewModel.epg.nowNext(current.epgId),
                isTv = isTv,
                onPrevious = { userZap { viewModel.previous() } },
                onNext = { userZap { viewModel.next() } },
                onGuide = { showGuide = true },
                modifier = Modifier.align(Alignment.BottomCenter),
            )
        }

        if (numberBuffer.isNotEmpty()) {
            Text(
                numberBuffer,
                color = Accent,
                fontFamily = PixelFont,
                fontSize = 32.sp,
                modifier = Modifier.align(Alignment.TopEnd).statusBarsPadding().padding(24.dp),
            )
        }

        if (showGuide) {
            GuideScreen(
                channels = channels,
                currentChannel = current,
                epg = viewModel.epg,
                favorites = viewModel.favorites,
                ads = ads,
                onTune = { ch ->
                    userZap { viewModel.select(channels.indexOf(ch)) }
                    showGuide = false
                },
                onToggleFavorite = viewModel::toggleFavorite,
            )
        }
    }
}

@Composable
private fun CenterMessage(text: String) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(text, color = Color.White, fontFamily = PixelFont, fontSize = 14.sp)
    }
}

@Composable
private fun InfoBar(
    channel: Channel,
    nowNext: NowNext,
    isTv: Boolean,
    onPrevious: () -> Unit,
    onNext: () -> Unit,
    onGuide: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val now by produceState(System.currentTimeMillis()) {
        while (true) {
            delay(15_000)
            value = System.currentTimeMillis()
        }
    }
    Column(
        modifier
            .fillMaxWidth()
            .background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xDD000020))))
            .navigationBarsPadding()
            .padding(horizontal = 24.dp, vertical = 16.dp),
    ) {
        Spacer(Modifier.height(24.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(channel.number.toString().padStart(2, '0'), color = Accent, fontFamily = PixelFont, fontSize = 22.sp)
            Spacer(Modifier.padding(horizontal = 8.dp))
            Text(channel.name, color = Color.White, fontFamily = PixelFont, fontSize = 16.sp, maxLines = 1)
        }
        Spacer(Modifier.height(8.dp))
        val prog = nowNext.now
        if (prog != null) {
            Text("${timeRange(prog.start, prog.stop)}  ${prog.displayTitle}", color = Color.White, fontSize = 15.sp, maxLines = 1)
            Spacer(Modifier.height(6.dp))
            LinearProgressIndicator(progress = { prog.progress(now) }, modifier = Modifier.fillMaxWidth().height(4.dp), color = Accent)
        } else {
            Text(channel.description.ifBlank { "No schedule available" }, color = Color(0xFFCCCCDD), fontSize = 14.sp, maxLines = 1)
        }
        nowNext.next?.let {
            Spacer(Modifier.height(6.dp))
            Text("NEXT  ${timeRange(it.start, it.stop)}  ${it.displayTitle}", color = Color(0xFFAAAACC), fontSize = 13.sp, maxLines = 1)
        }
        if (!isTv) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = androidx.compose.foundation.layout.Arrangement.SpaceBetween) {
                TextButton(onClick = onPrevious) { Text("▼ CH") }
                TextButton(onClick = onGuide) { Text("GUIDE") }
                TextButton(onClick = onNext) { Text("CH ▲") }
            }
        }
    }
}

private fun timeRange(startMs: Long, stopMs: Long): String {
    val f = DateFormat.getTimeInstance(DateFormat.SHORT)
    return "${f.format(Date(startMs))}–${f.format(Date(stopMs))}"
}
