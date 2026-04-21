package os.sneha.notification

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import os.sneha.BuildConfig
import os.sneha.MainActivity
import os.sneha.R
import os.sneha.data.SnehaApi

/**
 * Fires at ~22:00 local. If tonight's night star isn't yet collected,
 * posts a local notification nudging the user to do their night
 * ritual. If it's already collected, does nothing.
 */
class NightReminderWorker(
    ctx: Context, params: WorkerParameters
) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val today = withContext(Dispatchers.IO) {
            SnehaApi(BuildConfig.BASE_URL).fetchToday()
        }.getOrNull()

        if (today == null) return Result.retry()
        if (today.nightStar) return Result.success()       // already done

        postNudge()
        return Result.success()
    }

    private fun postNudge() {
        val ctx = applicationContext
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Night ritual reminder",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply { description = "10 PM nudge if you haven't collected your night star yet." }
            nm.createNotificationChannel(channel)
        }

        val tapIntent = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notif = NotificationCompat.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("🌙 Night ritual pending")
            .setContentText("Tap to finish the day — collect your night star.")
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setContentIntent(tapIntent)
            .build()

        NotificationManagerCompat.from(ctx).notify(NOTIF_ID, notif)
    }

    companion object {
        const val CHANNEL_ID = "night_ritual"
        const val NOTIF_ID = 1001
    }
}
