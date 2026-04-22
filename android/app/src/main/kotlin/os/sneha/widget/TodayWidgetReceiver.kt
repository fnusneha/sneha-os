package os.sneha.widget

import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver

/**
 * AppWidget host entry point. Extends GlanceAppWidgetReceiver which
 * handles the standard lifecycle (onEnabled / onDisabled / onUpdate /
 * onDeleted) and delegates rendering to `glanceAppWidget`.
 *
 * We also override onUpdate to trigger a WorkManager-backed refresh
 * whenever the system asks the widget to update (every 30 min by
 * default per `today_widget_info.xml`, plus on app install, reboot,
 * pin/unpin). That's a third independent path to "the tile shows
 * fresh data" alongside the WorkManager periodic job and the direct
 * refresh from `MainActivity.onResume`.
 */
class TodayWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = TodayWidget()

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
    ) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
        try {
            WidgetUpdateScheduler.schedule(context)
        } catch (t: Throwable) {
            android.util.Log.w("SnehaOSWidget", "receiver onUpdate schedule failed: ${t.message}")
        }
    }

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        // Fired when the first widget instance is placed. Kick a
        // refresh immediately so the freshly-placed tile doesn't sit
        // showing defaults ("0 / 3 stars") until the next periodic.
        try {
            WidgetUpdateScheduler.schedule(context)
        } catch (t: Throwable) {
            android.util.Log.w("SnehaOSWidget", "receiver onEnabled schedule failed: ${t.message}")
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        // Cover APPWIDGET_ENABLED + APPWIDGET_UPDATE broadcasts the
        // GlanceAppWidgetReceiver parent may silently short-circuit —
        // defensively re-schedule on every receive.
        try {
            WidgetUpdateScheduler.schedule(context)
        } catch (t: Throwable) {
            android.util.Log.w("SnehaOSWidget", "receiver onReceive schedule failed: ${t.message}")
        }
    }
}
