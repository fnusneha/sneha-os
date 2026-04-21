package os.sneha.notification

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Re-schedules the nightly reminder after a device reboot.
 * WorkManager persists periodic work by default, but explicit boot
 * reschedule handles edge cases where the OS clears scheduled jobs.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            NightReminderScheduler.schedule(context)
        }
    }
}
