package os.sneha.widget

import android.content.Context
import android.util.Log
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import os.sneha.BuildConfig
import os.sneha.data.SnehaApi

/**
 * Periodic worker that refreshes the home-screen Today widget from
 * `/api/today?force=1`. Scheduled by `WidgetUpdateScheduler`.
 *
 * Why `force=1`: the backend has a 60s in-memory cache on live Oura
 * + Garmin fetches. Without force, two widget refreshes within the
 * same minute would see the same numbers. Force bypasses it so every
 * widget update genuinely pulls from upstream.
 */
class WidgetUpdateWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    companion object {
        private const val TAG = "SnehaOSWidget"
    }

    override suspend fun doWork(): Result {
        val api = SnehaApi(BuildConfig.BASE_URL)
        val today = api.fetchToday(force = true).getOrNull()
            ?: run {
                Log.w(TAG, "fetchToday returned null — will retry")
                return Result.retry()
            }

        val manager = GlanceAppWidgetManager(applicationContext)
        val glanceIds = manager.getGlanceIds(TodayWidget::class.java)
        if (glanceIds.isEmpty()) {
            Log.i(TAG, "no widget instances placed — skipping update")
            return Result.success()
        }

        val coreEarned = today.coreDone >= today.coreThreshold
        Log.i(
            TAG,
            "update: steps=${today.steps} left=${today.stepsLeft} " +
                "stars=${today.starsToday}/3 week=${today.starsWeek} " +
                "cal=${today.calories ?: 0}/${today.calorieGoal} " +
                "widgets=${glanceIds.size}"
        )

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
