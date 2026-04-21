package os.sneha.notification

import android.content.Context
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.time.Duration
import java.time.LocalDateTime
import java.time.LocalTime
import java.util.concurrent.TimeUnit

/**
 * Schedules the 10 PM night-ritual reminder as a periodic
 * WorkManager task. Idempotent — calling schedule() multiple times
 * replaces the existing schedule, never duplicates.
 */
object NightReminderScheduler {
    private const val WORK_NAME = "night_ritual_reminder"

    fun schedule(context: Context) {
        val now = LocalDateTime.now()
        val target = now.with(LocalTime.of(22, 0))
        val delayDuration = Duration.between(
            now,
            if (target.isBefore(now)) target.plusDays(1) else target
        )

        val request = PeriodicWorkRequestBuilder<NightReminderWorker>(
            1, TimeUnit.DAYS
        )
            .setInitialDelay(delayDuration.toMinutes(), TimeUnit.MINUTES)
            .build()

        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
    }
}
