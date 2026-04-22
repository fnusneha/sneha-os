package os.sneha.widget

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * Enqueues a periodic WorkManager job that refreshes the Today widget
 * from `/api/today` every 30 min while the device has network.
 *
 * 30 min is WorkManager's minimum for periodic work (anything shorter
 * gets coalesced to 15 min by the system). It balances freshness
 * against battery — the widget tile will lag at most half an hour
 * behind reality, and the user can always tap ⟳ for an instant update.
 */
object WidgetUpdateScheduler {
    private const val WORK_NAME = "today_widget_refresh"
    private const val INTERVAL_MIN = 30L

    fun schedule(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        val request = PeriodicWorkRequestBuilder<WidgetUpdateWorker>(
            INTERVAL_MIN, TimeUnit.MINUTES
        )
            .setConstraints(constraints)
            // Tiny initial delay so we don't hammer Render right at app
            // cold-start; the first RefreshTodayAction already covers that.
            .setInitialDelay(2, TimeUnit.MINUTES)
            .build()

        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
    }
}
