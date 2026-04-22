package os.sneha

import android.app.Application
import os.sneha.notification.NightReminderScheduler
import os.sneha.widget.WidgetUpdateScheduler

/**
 * Application class. Enqueues two periodic WorkManager jobs on first
 * launch — WorkManager deduplicates by name, so repeat calls are no-ops.
 *
 *   - NightReminderScheduler  → daily 10 PM nudge if night-star pending
 *   - WidgetUpdateScheduler   → every 30 min refresh of the Today widget
 */
class SnehaOSApp : Application() {
    override fun onCreate() {
        super.onCreate()
        NightReminderScheduler.schedule(this)
        WidgetUpdateScheduler.schedule(this)
    }
}
