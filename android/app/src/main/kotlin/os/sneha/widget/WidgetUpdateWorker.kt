package os.sneha.widget

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters

/**
 * WorkManager wrapper around WidgetRefresh.refreshAll. Scheduled by
 * WidgetUpdateScheduler — immediate one-shot on app open, then every
 * 15 min while the device has network. Returns `retry()` on failure
 * so transient flakes don't leave the widget stale.
 */
class WidgetUpdateWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result =
        if (WidgetRefresh.refreshAll(applicationContext)) Result.success()
        else Result.retry()
}
