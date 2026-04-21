package os.sneha

import android.app.Application
import os.sneha.notification.NightReminderScheduler

/**
 * Application class. Schedules the daily 10 PM "night ritual pending?"
 * reminder as a periodic WorkManager job the first time the app launches.
 * WorkManager deduplicates by name, so repeat calls are no-ops.
 */
class SnehaOSApp : Application() {
    override fun onCreate() {
        super.onCreate()
        NightReminderScheduler.schedule(this)
    }
}
