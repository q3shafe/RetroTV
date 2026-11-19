package com.shafe.retrotv.data

import android.util.Log
import android.util.Xml
import org.xmlpull.v1.XmlPullParser
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.Locale

/** Streams an XMLTV guide and keeps only the programmes we can actually show. */
object EpgRepository {
    const val EPG_URL = "https://cdn.wedovids.com/tv/guide.xml"
    private const val TAG = "EpgRepo"
    private const val PAST_MS = 6L * 3600_000
    private const val FUTURE_MS = 36L * 3600_000

    fun load(wantedIds: Set<String>): Epg =
        runCatching { Http.open(EPG_URL, 60_000).use { parse(it, wantedIds) } }
            .onFailure { Log.w(TAG, "EPG load failed: $it") }
            .getOrDefault(emptyMap())

    fun parse(input: InputStream, wantedIds: Set<String>, nowMs: Long = System.currentTimeMillis()): Epg {
        val out = HashMap<String, MutableList<Programme>>()
        val p = Xml.newPullParser()
        p.setInput(input, null)
        var ev = p.eventType
        while (ev != XmlPullParser.END_DOCUMENT) {
            if (ev == XmlPullParser.START_TAG && p.name == "programme") {
                val id = p.getAttributeValue(null, "channel").orEmpty()
                val start = parseTime(p.getAttributeValue(null, "start"))
                val stop = parseTime(p.getAttributeValue(null, "stop"))
                val prog = readProgramme(p, start, stop)
                if (id in wantedIds && prog != null &&
                    prog.stop > nowMs - PAST_MS && prog.start < nowMs + FUTURE_MS
                ) out.getOrPut(id) { mutableListOf() }.add(prog)
            }
            ev = p.next()
        }
        out.values.forEach { l -> l.sortBy { it.start } }
        return out
    }

    private fun readProgramme(p: XmlPullParser, start: Long?, stop: Long?): Programme? {
        var title = ""
        var sub = ""
        var desc = ""
        var cat = ""
        loop@ while (true) {
            when (p.next()) {
                XmlPullParser.START_TAG -> when (p.name) {
                    "title" -> title = p.nextText().trim()
                    "sub-title" -> sub = p.nextText().trim()
                    "desc" -> desc = p.nextText().trim()
                    "category" -> { val c = p.nextText().trim(); if (cat.isEmpty()) cat = c }
                }
                XmlPullParser.END_TAG -> if (p.name == "programme") break@loop
                XmlPullParser.END_DOCUMENT -> break@loop
            }
        }
        if (start == null) return null
        return Programme(start, stop ?: (start + 30 * 60_000), title, sub, desc, cat)
    }

    /** Parses '20240328060000 +0000' (also '+05:30'). SimpleDateFormat is not thread safe, so one per call. */
    fun parseTime(s: String?): Long? {
        if (s.isNullOrBlank()) return null
        val parts = s.trim().split(' ')
        val ts = parts[0].padEnd(14, '0').take(14)
        val tz = (parts.getOrNull(1) ?: "+0000").replace(":", "")
        return runCatching {
            SimpleDateFormat("yyyyMMddHHmmss Z", Locale.US).parse("$ts $tz")?.time
        }.getOrNull()
    }
}
