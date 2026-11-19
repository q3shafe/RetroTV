package com.shafe.retrotv.data

/** A live channel. Mirrors the desktop Channel class, minus local playlists and commercials. */
data class Channel(
    val number: Int,
    val name: String,
    val url: String,
    val description: String = "",
    /** Matches the programme channel attribute in the XMLTV guide. */
    val epgId: String? = null,
)

/** Times are epoch milliseconds (UTC). */
data class Programme(
    val start: Long,
    val stop: Long,
    val title: String,
    val subtitle: String = "",
    val desc: String = "",
    val category: String = "",
) {
    /** Same rule as the desktop app: use the subtitle when the title is generic. */
    val displayTitle: String
        get() = when {
            title.uppercase() in setOf("MOVIE", "FILM", "FEATURE", "") && subtitle.isNotEmpty() -> subtitle
            subtitle.isNotEmpty() -> "$title: $subtitle"
            else -> title
        }

    fun progress(now: Long): Float {
        val total = stop - start
        if (total <= 0) return 0f
        return ((now - start).toFloat() / total).coerceIn(0f, 1f)
    }
}

data class NowNext(val now: Programme?, val next: Programme?)

typealias Epg = Map<String, List<Programme>>

fun Epg.nowNext(epgId: String?, nowMs: Long = System.currentTimeMillis()): NowNext {
    val progs = epgId?.let { this[it] } ?: return NowNext(null, null)
    val idx = progs.indexOfFirst { it.start <= nowMs && it.stop > nowMs }
    return when {
        idx >= 0 -> NowNext(progs[idx], progs.getOrNull(idx + 1))
        else -> NowNext(null, progs.firstOrNull { it.start > nowMs })
    }
}
