package com.shafe.retrotv.data

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Loads the curated live channel list (the same file the desktop app uses).
 * Order of preference: fresh download, last cached copy, bundled asset.
 * Entries with a "playlist" key (local files) are ignored: live channels only.
 */
object ChannelRepository {
    const val CONFIG_URL = "https://cdn.wedovids.com/tv/tv_config.json"
    private const val CACHE_NAME = "tv_config.json"
    private const val TAG = "ChannelRepo"

    fun load(context: Context): List<Channel> {
        val cache = File(context.filesDir, CACHE_NAME)
        val remote = runCatching { Http.getText(CONFIG_URL) }
            .onFailure { Log.w(TAG, "remote config failed: $it") }
            .getOrNull()
        if (remote != null) {
            val parsed = runCatching { parse(remote) }.getOrNull()
            if (!parsed.isNullOrEmpty()) {
                runCatching { cache.writeText(remote) }
                return parsed
            }
        }
        if (cache.exists()) {
            runCatching { parse(cache.readText()) }.getOrNull()
                ?.takeIf { it.isNotEmpty() }
                ?.let { return it }
        }
        return runCatching {
            parse(context.assets.open(CACHE_NAME).bufferedReader().use { it.readText() })
        }.getOrDefault(emptyList())
    }

    /** Accepts {"channels": [[...]]}, {"channels": [...]} or a bare list; tolerates trailing commas. */
    fun parse(raw: String): List<Channel> {
        val cleaned = raw.replace(Regex(",\\s*([\\]}])"), "$1").trim()
        val items: JSONArray =
            if (cleaned.startsWith("[")) JSONArray(cleaned)
            else JSONObject(cleaned).optJSONArray("channels") ?: JSONArray()

        val flat = mutableListOf<JSONObject>()
        for (i in 0 until items.length()) {
            when (val item = items.get(i)) {
                is JSONArray -> for (j in 0 until item.length()) item.optJSONObject(j)?.let(flat::add)
                is JSONObject -> flat.add(item)
            }
        }
        var number = 1
        return flat.mapNotNull { o ->
            val url = o.optString("url").trim()
            if (url.isEmpty() || o.optString("playlist").isNotBlank()) return@mapNotNull null
            Channel(
                number = number,
                name = o.optString("name").ifBlank { "CH$number" },
                url = url,
                description = o.optString("description").trim(),
                epgId = o.optString("channel-id").trim().ifEmpty { null },
            ).also { number++ }
        }
    }
}
