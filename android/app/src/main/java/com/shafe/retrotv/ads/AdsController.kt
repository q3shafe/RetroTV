package com.shafe.retrotv.ads

import android.app.Activity
import android.os.SystemClock
import android.util.Log
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.wrapContentHeight
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.viewinterop.AndroidView
import com.google.android.gms.ads.AdListener
import com.google.android.gms.ads.AdRequest
import com.google.android.gms.ads.AdSize
import com.google.android.gms.ads.AdView
import com.google.android.gms.ads.FullScreenContentCallback
import com.google.android.gms.ads.LoadAdError
import com.google.android.gms.ads.MobileAds
import com.google.android.gms.ads.interstitial.InterstitialAd
import com.google.android.gms.ads.interstitial.InterstitialAdLoadCallback
import com.google.android.ump.ConsentRequestParameters
import com.google.android.ump.UserMessagingPlatform
import com.shafe.retrotv.BuildConfig
import java.util.concurrent.atomic.AtomicBoolean

/**
 * AdMob wiring. Phones only: the Google Mobile Ads SDK does not officially support Android TV,
 * so MainActivity passes enabled = false on TV devices and nothing in here ever runs.
 *
 * Flow: UMP consent (required in the EEA/UK) -> MobileAds.initialize -> banner + interstitial.
 */
class AdsController(private val activity: Activity, val enabled: Boolean) {
    /** True once consent allows ad requests and the SDK has initialised. */
    var ready by mutableStateOf(false)
        private set

    private val initStarted = AtomicBoolean(false)
    private var interstitial: InterstitialAd? = null
    private var channelChanges = 0
    private var lastInterstitialAt = -INTERSTITIAL_MIN_GAP_MS

    fun start() {
        if (!enabled) return
        val consent = UserMessagingPlatform.getConsentInformation(activity)
        consent.requestConsentInfoUpdate(
            activity,
            ConsentRequestParameters.Builder().build(),
            {
                UserMessagingPlatform.loadAndShowConsentFormIfRequired(activity) { err ->
                    if (err != null) Log.w(TAG, "consent form: ${err.message}")
                    if (consent.canRequestAds()) initAds()
                }
            },
            { err ->
                Log.w(TAG, "consent update: ${err.message}")
                if (consent.canRequestAds()) initAds()
            },
        )
        // Consent from a previous session may already allow ads.
        if (consent.canRequestAds()) initAds()
    }

    private fun initAds() {
        if (!initStarted.compareAndSet(false, true)) return
        MobileAds.initialize(activity) {
            ready = true
            loadInterstitial()
        }
    }

    private fun loadInterstitial() {
        InterstitialAd.load(
            activity,
            BuildConfig.ADMOB_INTERSTITIAL_ID,
            AdRequest.Builder().build(),
            object : InterstitialAdLoadCallback() {
                override fun onAdLoaded(ad: InterstitialAd) {
                    interstitial = ad
                }

                override fun onAdFailedToLoad(error: LoadAdError) {
                    Log.w(TAG, "interstitial load failed: ${error.message}")
                    interstitial = null
                }
            },
        )
    }

    /**
     * Call on every user-initiated channel change. Shows a full-screen ad at most once per
     * [INTERSTITIAL_EVERY] changes and never more often than [INTERSTITIAL_MIN_GAP_MS].
     */
    fun onChannelChanged() {
        if (!enabled || !ready) return
        channelChanges++
        val ad = interstitial ?: return
        val now = SystemClock.elapsedRealtime()
        if (channelChanges < INTERSTITIAL_EVERY || now - lastInterstitialAt < INTERSTITIAL_MIN_GAP_MS) return
        channelChanges = 0
        lastInterstitialAt = now
        ad.fullScreenContentCallback = object : FullScreenContentCallback() {
            override fun onAdDismissedFullScreenContent() {
                interstitial = null
                loadInterstitial()
            }

            override fun onAdFailedToShowFullScreenContent(error: com.google.android.gms.ads.AdError) {
                interstitial = null
                loadInterstitial()
            }
        }
        ad.show(activity)
    }

    private companion object {
        const val TAG = "AdsController"
        const val INTERSTITIAL_EVERY = 8
        const val INTERSTITIAL_MIN_GAP_MS = 3 * 60_000L
    }
}

/** Anchored adaptive banner. Place it beside the video, never on top of it. */
@Composable
fun AdBanner(modifier: Modifier = Modifier) {
    val widthDp = LocalConfiguration.current.screenWidthDp
    AndroidView(
        modifier = modifier.fillMaxWidth().wrapContentHeight(),
        factory = { ctx ->
            AdView(ctx).apply {
                setAdSize(AdSize.getCurrentOrientationAnchoredAdaptiveBannerAdSize(ctx, widthDp))
                adUnitId = BuildConfig.ADMOB_BANNER_ID
                adListener = object : AdListener() {
                    override fun onAdFailedToLoad(error: LoadAdError) {
                        Log.w("AdBanner", "banner failed: ${error.message}")
                    }
                }
                loadAd(AdRequest.Builder().build())
            }
        },
        onRelease = { it.destroy() },
    )
}
