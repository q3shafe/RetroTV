package com.shafe.retrotv.data

import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

object Http {
    const val USER_AGENT = "RetroTV-Android/0.1"

    fun open(url: String, timeoutMs: Int = 20_000): InputStream {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = timeoutMs
        c.readTimeout = timeoutMs
        c.instanceFollowRedirects = true
        c.setRequestProperty("User-Agent", USER_AGENT)
        if (c.responseCode !in 200..299) error("HTTP ${c.responseCode} for $url")
        return c.inputStream
    }

    fun getText(url: String): String = open(url).bufferedReader().use { it.readText() }
}
