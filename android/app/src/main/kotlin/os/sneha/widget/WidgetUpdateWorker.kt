package os.sneha.widget

import android.content.Context
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import os.sneha.BuildConfig
import os.sneha.data.SnehaApi

/**
 * Periodic worker that refreshes the home-screen Today widget from
 * `/api/today`. Scheduled by `WidgetUpdateScheduler` on app start.
 *
 * Why: without this, the widget only updates when the user taps the
 * refresh glyph or reopens the app — so opening the phone at 3pm
 * still shows the morning's snapshot. Running this every 30 min keeps
 * it roughly current without draining the battery.
 */
class WidgetUpdateWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val today = SnehaApi(BuildConfig.BASE_URL).fetchToday().getOrNull()
            ?: return Result.retry()

        val manager = GlanceAppWidgetManager(applicationContext)
        val glanceIds = manager.getGlanceIds(TodayWidget::class.java)
        if (glanceIds.isEmpty()) return Result.success()  // no widget placed

        val coreEarned = today.coreDone >= today.coreThreshold

        glanceIds.forEach { id ->
            updateAppWidgetState(
                applicationContext,
                PreferencesGlanceStateDefinition,
                id,
            ) { prefs ->
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
                }
            }
            TodayWidget().update(applicationContext, id)
        }
        return Result.success()
    }
}
