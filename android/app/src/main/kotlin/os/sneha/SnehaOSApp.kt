package os.sneha

import android.app.Application
import android.util.Log
import os.sneha.notification.NightReminderScheduler
import os.sneha.widget.WidgetUpdateScheduler

/**
 * Application class. Enqueues two periodic WorkManager jobs on first
 * launch — WorkManager deduplicates by name, so repeat calls are no-ops.
 *
 * Both scheduler calls are wrapped in try/catch: if WorkManager fails
 * to initialise (known flake on some Android 15 builds when the app
 * isn't yet "running" by the system's definition), we must NOT take
 * the whole app down. The schedulers will get another chance to run
 * from MainActivity.onResume.
 */
class SnehaOSApp : Application() {
    override fun onCreate() {
        super.onCreate()
        try {
            NightReminderScheduler.schedule(this)
        } catch (t: Throwable) {
            Log.w("SnehaOS", "night reminder scheduler failed at app start: ${t.message}")
        }
        try {
            WidgetUpdateScheduler.schedule(this)
        } catch (t: Throwable) {
            Log.w("SnehaOS", "widget scheduler failed at app start: ${t.message}")
        }
    }
}
