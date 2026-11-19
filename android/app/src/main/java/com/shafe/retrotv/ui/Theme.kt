package com.shafe.retrotv.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import com.shafe.retrotv.R

val PixelFont = FontFamily(Font(R.font.pressstart2p))

val Accent = Color(0xFFFF9900)
val Panel = Color(0xE6101030)

@Composable
fun RetroTvTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = Accent,
            background = Color.Black,
            surface = Color(0xFF101030),
        ),
        content = content,
    )
}
