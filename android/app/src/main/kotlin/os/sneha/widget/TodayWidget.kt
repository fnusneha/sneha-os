package os.sneha.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.TextUnitType
import androidx.compose.ui.unit.dp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.glance.ColorFilter
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.background
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.state.GlanceStateDefinition
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import os.sneha.BuildConfig
import os.sneha.MainActivity
import os.sneha.data.SnehaApi

/**
 * Home-screen widget. Two lines: today's star count + steps-left.
 * Tap body → opens app. Tap ⟳ → force refresh from `/api/today`.
 */
class TodayWidget : GlanceAppWidget() {

    override val stateDefinition: GlanceStateDefinition<*> =
        PreferencesGlanceStateDefinition

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        // Self-populate on every render — we know `id`, so refreshFor
        // is narrower than refreshAll (no enumeration, no extra
        // update() call because we're already in a composition).
        try {
            WidgetRefresh.refreshFor(context, id)
        } catch (t: Throwable) {
            android.util.Log.w("SnehaOSWidget", "provideGlance refresh failed: ${t.message}")
        }
        provideContent {
            GlanceTheme { Body() }
        }
    }

    @Composable
    private fun Body() {
        val prefs = currentState<Preferences>()
        val steps = prefs[Keys.STEPS] ?: 0
        val stepsLeft = prefs[Keys.STEPS_LEFT] ?: 8000
        val starsToday = prefs[Keys.STARS_TODAY] ?: 0
        val starsWeek = prefs[Keys.STARS_WEEK] ?: 0
        val lastRefresh = prefs[Keys.LAST_REFRESH] ?: 0L
        val updatedLabel = if (lastRefresh > 0L) {
            // Format as HH:mm in device local time.
            val formatter = java.text.SimpleDateFormat("h:mm a", java.util.Locale.getDefault())
            "updated " + formatter.format(java.util.Date(lastRefresh))
        } else {
            "never refreshed"
        }

        Column(
            modifier = GlanceModifier
                .fillMaxSize()
                .background(bg)
                .cornerRadius(20.dp)
                .padding(14.dp)
                .clickable(actionStartActivity<MainActivity>())
        ) {
            Row(
                modifier = GlanceModifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("TODAY", style = labelStyle(muted))
                Spacer(GlanceModifier.defaultWeight())
                // Larger clickable target (padding around the glyph)
                // — 14sp text with no padding was a ~6dp hit area, too
                // small for a reliable tap. padding(6dp) roughly
                // doubles the hit box.
                Text(
                    "\u27f3",
                    style = labelStyle(mint, size = 18f),
                    modifier = GlanceModifier
                        .padding(6.dp)
                        .clickable(actionRunCallback<RefreshTodayAction>())
                )
            }
            Spacer(GlanceModifier.height(6.dp))

            Row(verticalAlignment = Alignment.Bottom) {
                Text(
                    "$starsToday",
                    style = TextStyle(
                        color = gold, fontSize = sp(36f), fontWeight = FontWeight.Bold
                    )
                )
                Text(" / 3 stars", style = labelStyle(dim, size = 12f))
            }
            Spacer(GlanceModifier.height(8.dp))
            Text(
                if (stepsLeft == 0) "$steps steps \u2713"
                else "$stepsLeft steps left",
                style = TextStyle(color = text, fontSize = sp(14f))
            )
            Spacer(GlanceModifier.height(2.dp))
            Text("$starsWeek this week", style = labelStyle(muted, size = 10f))
            Spacer(GlanceModifier.height(4.dp))
            // Diagnostic footer: proves a refresh actually ran. If the
            // numbers above look wrong but this timestamp IS current,
            // the server returned those numbers. If this timestamp is
            // stale or "never refreshed", the refresh path isn't firing.
            Text(updatedLabel, style = labelStyle(dim, size = 9f))
        }
    }

    companion object {
        // Glance unit.ColorProvider takes a Compose Color directly.
        val bg = ColorProvider(Color(0xFF141F33))
        val mint = ColorProvider(Color(0xFF6EE7B7))
        val gold = ColorProvider(Color(0xFFF5C842))
        val text = ColorProvider(Color(0xFFE8EEF5))
        val muted = ColorProvider(Color(0xFF7A9AB8))
        val dim = ColorProvider(Color(0xFF3D5A77))

        fun sp(v: Float) = TextUnit(v, TextUnitType.Sp)
        fun labelStyle(color: ColorProvider, size: Float = 10f) = TextStyle(
            color = color, fontSize = sp(size), fontWeight = FontWeight.Medium
        )
    }
}

object Keys {
    val STEPS = intPreferencesKey("steps")
    val STEPS_LEFT = intPreferencesKey("stepsLeft")
    val STARS_TODAY = intPreferencesKey("starsToday")
    val STARS_WEEK = intPreferencesKey("starsWeek")
    val MORNING = booleanPreferencesKey("morning")
    val CORE = booleanPreferencesKey("core")
    val NIGHT = booleanPreferencesKey("night")
    val CYCLE = stringPreferencesKey("cycle")
    // Epoch millis of the last successful refresh. Used for the
    // "updated HH:mm" footer on the tile — makes it obvious whether
    // a tap-refresh actually landed, separate from whether the server
    // returned fresh numbers.
    val LAST_REFRESH = androidx.datastore.preferences.core.longPreferencesKey("lastRefresh")
}

/**
 * Single source of truth for "fetch /api/today and push into widget
 * state". Used by three call sites:
 *   • RefreshTodayAction  (user taps the ⟳ glyph on the tile)
 *   • WidgetUpdateWorker   (WorkManager periodic / one-shot)
 *   • MainActivity.onResume (direct refresh whenever app foregrounds,
 *                            bypassing WorkManager flakiness)
 *
 * Always calls /api/today?force=1 so the backend's 60s live-fetch
 * cache doesn't serve a near-stale snapshot to the widget.
 */
object WidgetRefresh {
    private const val TAG = "SnehaOSWidget"

    /** Write fresh data to a single GlanceId's state. */
    private suspend fun writeState(
        context: android.content.Context,
        id: GlanceId,
        today: os.sneha.data.TodayDto,
    ) {
        val coreEarned = today.coreDone >= today.coreThreshold
        val now = System.currentTimeMillis()
        updateAppWidgetState(context, PreferencesGlanceStateDefinition, id) { prefs ->
            prefs.toMutablePreferences().apply {
                this[Keys.STEPS] = today.steps
                this[Keys.STEPS_LEFT] = today.stepsLeft
                this[Keys.STARS_TODAY] = today.starsToday
                this[Keys.STARS_WEEK] = today.starsWeek
                this[Keys.MORNING] = today.morningStar
                this[Keys.CORE] = coreEarned
                this[Keys.NIGHT] = today.nightStar
                this[Keys.CYCLE] =
                    if (today.cyclePhase.isBlank()) ""
                    else today.cyclePhase + (today.cycleDay?.let { " D$it" } ?: "")
                this[Keys.LAST_REFRESH] = now
            }
        }
    }

    /**
     * Fetch and write state for a SINGLE GlanceId. Called from
     * TodayWidget.provideGlance — we already know which widget is
     * being composed, no need to enumerate. Does NOT call update()
     * because we're already inside a composition.
     */
    suspend fun refreshFor(context: android.content.Context, id: GlanceId): Boolean {
        val today = SnehaApi(BuildConfig.BASE_URL).fetchToday(force = true).getOrNull()
        if (today == null) {
            android.util.Log.w(TAG, "refreshFor($id): fetchToday null")
            return false
        }
        android.util.Log.i(
            TAG,
            "refreshFor($id): steps=${today.steps} stars=${today.starsToday}/3 " +
                "morning=${today.morningStar} cal=${today.calories ?: 0}"
        )
        writeState(context, id, today)
        return true
    }

    /**
     * Fetch once, write to EVERY placed widget instance, then ping
     * update() so Glance re-renders them. Used by the tap-refresh
     * ActionCallback and by the WorkManager worker.
     */
    suspend fun refreshAll(context: android.content.Context): Boolean {
        val today = SnehaApi(BuildConfig.BASE_URL).fetchToday(force = true).getOrNull()
        if (today == null) {
            android.util.Log.w(TAG, "refreshAll: fetchToday null")
            return false
        }
        val manager = androidx.glance.appwidget.GlanceAppWidgetManager(context)
        val ids = manager.getGlanceIds(TodayWidget::class.java)
        android.util.Log.i(
            TAG,
            "refreshAll: widgets=${ids.size} steps=${today.steps} " +
                "stars=${today.starsToday}/3 morning=${today.morningStar} " +
                "cal=${today.calories ?: 0}"
        )
        ids.forEach { id ->
            writeState(context, id, today)
            TodayWidget().update(context, id)
        }
        return true
    }
}

/** Tap-to-refresh: fetch /api/today and update widget state. */
class RefreshTodayAction : ActionCallback {
    override suspend fun onAction(
        context: Context,
        glanceId: GlanceId,
        parameters: androidx.glance.action.ActionParameters,
    ) {
        withContext(Dispatchers.IO) { WidgetRefresh.refreshAll(context) }
    }
}
