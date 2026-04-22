package os.sneha.widget

import android.content.Context
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * Two-tier widget refresh:
 *
 *   • An immediate one-shot when the app starts so the tile is fresh
 *     the moment the user returns to the home screen from the app.
 *   • A 15 min periodic job (WorkManager's floor) while the device has
 *     network, so the tile stays current while the app is closed.
 *
 * Everything is deduped by work name — calling this repeatedly is
 * safe and cheap.
 */
object WidgetUpdateScheduler {
    private const val WORK_NAME_PERIODIC = "today_widget_refresh"
    private const val WORK_NAME_IMMEDIATE = "today_widget_refresh_now"
    private const val INTERVAL_MIN = 15L
    private const val TAG = "SnehaOSWidget"

    fun schedule(context: Context) {
        val wm = WorkManager.getInstance(context)
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        // 1. Immediate one-off — fires right away (battery-optimised apps
        //    still get this under a minute). Doesn't wait for the 15-min
        //    periodic floor so the user's first look at the home-screen
        //    after opening the app is guaranteed fresh.
        val immediate = OneTimeWorkRequestBuilder<WidgetUpdateWorker>()
            .setConstraints(constraints)
            .build()
        wm.enqueueUniqueWork(
            WORK_NAME_IMMEDIATE,
            ExistingWorkPolicy.REPLACE,
            immediate,
        )

        // 2. Periodic every 15 min (the WorkManager minimum).
        val periodic = PeriodicWorkRequestBuilder<WidgetUpdateWorker>(
            INTERVAL_MIN, TimeUnit.MINUTES
        )
            .setConstraints(constraints)
            .build()
        wm.enqueueUniquePeriodicWork(
            WORK_NAME_PERIODIC,
            ExistingPeriodicWorkPolicy.UPDATE,
            periodic,
        )
        Log.i(TAG, "widget refresh scheduled (immediate + ${INTERVAL_MIN}min periodic)")
    }
}
