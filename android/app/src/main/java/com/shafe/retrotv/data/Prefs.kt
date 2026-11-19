package com.shafe.retrotv.data

import android.content.Context

class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("retrotv", Context.MODE_PRIVATE)

    var lastChannelName: String?
        get() = sp.getString("last_channel", null)
        set(v) = sp.edit().putString("last_channel", v).apply()

    var favorites: Set<String>
        get() = sp.getStringSet("favorites", emptySet()) ?: emptySet()
        set(v) = sp.edit().putStringSet("favorites", v.toSet()).apply()
}
