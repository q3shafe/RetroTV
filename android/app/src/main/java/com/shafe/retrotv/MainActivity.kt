package com.shafe.retrotv

import android.app.UiModeManager
import android.content.res.Configuration
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.shafe.retrotv.ads.AdsController
import com.shafe.retrotv.ui.RetroTvApp
import com.shafe.retrotv.ui.RetroTvTheme

class MainActivity : ComponentActivity() {
    private val viewModel: TvViewModel by viewModels()
    private lateinit var ads: AdsController

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val isTv = (getSystemService(UI_MODE_SERVICE) as UiModeManager).currentModeType ==
            Configuration.UI_MODE_TYPE_TELEVISION

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            hide(WindowInsetsCompat.Type.systemBars())
        }

        // AdMob is phone-only (not supported on Android TV).
        ads = AdsController(this, enabled = !isTv)
        ads.start()

        setContent {
            RetroTvTheme {
                RetroTvApp(viewModel = viewModel, ads = ads, isTv = isTv)
            }
        }
    }
}
