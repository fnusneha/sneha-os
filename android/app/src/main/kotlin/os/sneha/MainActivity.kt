package os.sneha

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.app.ActivityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * The whole UI is the Flask dashboard served by the backend — so the
 * Android app is a hardened WebView wrapper that renders it, plus a
 * pull-to-refresh and a sensible back-stack behaviour.
 *
 * Everything fancy (home-screen widget, 10 PM reminder) lives in native
 * code so it doesn't have to fight with WebView sandboxing.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var refreshLayout: SwipeRefreshLayout

    // Retry state for Render cold-starts. When a page-load errors, we
    // schedule a retry on the main Looper (so it runs after the current
    // onReceivedError callback returns) instead of immediately giving
    // up and showing the offline page.
    private val mainHandler = Handler(Looper.getMainLooper())
    private var retryRunnable: Runnable? = null
    private var retryAttempt = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(
            "SnehaOS",
            "build=${BuildConfig.GIT_SHA} " +
            "time=${BuildConfig.BUILD_TIME} " +
            "base=${BuildConfig.BASE_URL}"
        )
        // Android 15 (targetSdk 35) forces edge-to-edge by default —
        // the deprecated `setDecorFitsSystemWindows(true)` no longer
        // pushes content below the status bar, so the WebView was
        // rendering its hero text under the system clock/icons.
        // Tell the system the window goes edge-to-edge and we'll
        // handle insets ourselves on the SwipeRefreshLayout root.
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.parseColor("#0d1b2e")
        window.navigationBarColor = Color.parseColor("#0d1b2e")
        setTheme(R.style.Theme_SnehaOS)

        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.webView)
        refreshLayout = findViewById(R.id.swipeRefresh)
        refreshLayout.setColorSchemeColors(
            Color.parseColor("#6ee7b7"), // mint
            Color.parseColor("#f5c842"), // gold
            Color.parseColor("#7dd3fc"), // sky
        )
        refreshLayout.setProgressBackgroundColorSchemeColor(Color.parseColor("#1a2644"))

        // Apply system-bar insets as padding on the root view so the
        // WebView sits between the status bar (top) and the nav bar
        // (bottom) instead of behind them. Uses system-bars insets
        // rather than just status-bar so a 3-button nav also clears.
        ViewCompat.setOnApplyWindowInsetsListener(refreshLayout) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.updatePadding(
                left = bars.left,
                top = bars.top,
                right = bars.right,
                bottom = bars.bottom,
            )
            WindowInsetsCompat.CONSUMED
        }

        configureWebView(webView)
        refreshLayout.setOnRefreshListener {
            // Pull-to-refresh just reloads /dashboard. The backend reads
            // straight from Postgres (scheduled `sync.py` cron does the
            // external-API pulls), so a reload is enough to pick up the
            // newest row. The `force=1` query param is a hint for future
            // on-demand sync wiring; today it's a no-op.
            webView.loadUrl(baseDashboardUrl(force = true))
        }

        // Modern Android-14 back-press handling
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        if (savedInstanceState == null) {
            webView.loadUrl(baseDashboardUrl(force = false))
        } else {
            webView.restoreState(savedInstanceState)
        }

        maybeRequestNotificationPermission()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onPause() {
        super.onPause()
        webView.onPause()
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
        // Re-enqueue the widget refresh so every foreground visit
        // triggers an immediate one-shot (see WidgetUpdateScheduler).
        // Application.onCreate only runs once per process lifetime —
        // we need a hook that fires every time the user opens the app.
        os.sneha.widget.WidgetUpdateScheduler.schedule(applicationContext)
    }

    override fun onDestroy() {
        cancelRetry()
        super.onDestroy()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    // ──────────────────────────────────────────────────────────────────

    private fun baseDashboardUrl(force: Boolean): String {
        val base = BuildConfig.BASE_URL
        return if (force) "$base/dashboard?force=1" else "$base/dashboard"
    }

    /**
     * Schedule a retry on Render cold-start errors.
     *
     * Render's free tier naps after 15 min of no traffic; the first
     * request after that times out in the WebView before the container
     * is done booting. Rather than immediately giving up and showing
     * the static "Waking up…" fallback, we retry [MAX_RETRIES] times
     * with an exponential backoff, giving the container ~90 s total to
     * boot. Between retries the user sees the fallback page, so they
     * know the app is alive and automatically recovering.
     */
    private fun scheduleRetryOrGiveUp(view: WebView) {
        cancelRetry()
        if (retryAttempt >= MAX_RETRIES) {
            // Budget exhausted — leave the offline page visible and wait
            // for the user to pull-to-refresh manually.
            Log.w("SnehaOS", "gave up after ${MAX_RETRIES + 1} load attempts")
            showOfflinePage(view)
            return
        }

        showOfflinePage(view)
        retryAttempt += 1
        val delayMs = RETRY_BACKOFFS_MS[retryAttempt - 1]
        Log.i("SnehaOS", "retry #${retryAttempt} in ${delayMs}ms")
        retryRunnable = Runnable {
            if (!isFinishing && !isDestroyed) {
                webView.loadUrl(baseDashboardUrl(force = false))
            }
        }.also { mainHandler.postDelayed(it, delayMs) }
    }

    private fun cancelRetry() {
        retryRunnable?.let { mainHandler.removeCallbacks(it) }
        retryRunnable = null
    }

    private fun showOfflinePage(view: WebView) {
        view.loadDataWithBaseURL(
            BuildConfig.BASE_URL,
            buildOfflineHtml(),
            "text/html", "utf-8", null
        )
    }

    private fun configureWebView(wv: WebView) {
        wv.setBackgroundColor(Color.parseColor("#0d1b2e"))

        val s: WebSettings = wv.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true                 // localStorage for routine ticks
        s.databaseEnabled = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = false
        s.mediaPlaybackRequiresUserGesture = true
        s.cacheMode = WebSettings.LOAD_DEFAULT

        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)

        wv.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView, request: WebResourceRequest
            ): Boolean {
                val uri: Uri = request.url
                // Keep navigation inside the WebView if it's our own origin.
                val baseHost = Uri.parse(BuildConfig.BASE_URL).host ?: return false
                if (uri.host == baseHost) return false
                // Anything else (Strava activity links, Google Docs, maps…)
                // goes out to Chrome Custom Tabs so auth + browser extensions
                // work normally.
                CustomTabsIntent.Builder().build().launchUrl(view.context, uri)
                return true
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                refreshLayout.isRefreshing = true
            }

            override fun onPageFinished(view: WebView, url: String) {
                refreshLayout.isRefreshing = false
                // A real page (not the data: offline fallback) loaded —
                // clear any pending retry and reset the counter.
                if (url.startsWith(BuildConfig.BASE_URL)) {
                    cancelRetry()
                    retryAttempt = 0
                }
            }

            override fun onReceivedError(
                view: WebView, request: WebResourceRequest, error: WebResourceError
            ) {
                if (!request.isForMainFrame) return
                refreshLayout.isRefreshing = false
                scheduleRetryOrGiveUp(view)
            }
        }
    }

    private fun maybeRequestNotificationPermission() {
        // Android 13+ requires runtime permission for POST_NOTIFICATIONS.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ActivityCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF
                )
            }
        }
    }

    companion object {
        private const val REQ_NOTIF = 42

        // Render cold-start retry budget. Four attempts, 10/20/30/30 s
        // apart = ~90 s total, which covers the typical 30–60 s boot
        // plus the first retry that usually times out mid-boot.
        private const val MAX_RETRIES = 4
        private val RETRY_BACKOFFS_MS = longArrayOf(10_000, 20_000, 30_000, 30_000)

        // Inline, no-network fallback shown when the WebView can't reach
        // the backend (e.g. cold-start + no internet). Intentionally
        // matches the dashboard's dark palette so the app never shows
        // a jarring white page. Footer carries the git SHA of the build
        // so bug reports can be pinned to a commit.
        fun buildOfflineHtml(): String = """
            <!doctype html><html><head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <style>
                html,body{height:100%;margin:0;background:#0d1b2e;color:#e8eef5;
                  font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
                  display:flex;flex-direction:column;align-items:center;
                  justify-content:center;text-align:center;padding:24px}
                h1{color:#6ee7b7;font-weight:500;letter-spacing:-0.01em;margin:0 0 10px}
                p{color:#7a9ab8;font-size:14px;line-height:1.5}
                .hint{color:#3d5a77;font-size:12px;margin-top:24px}
                .ver{position:fixed;bottom:12px;left:0;right:0;color:#3d5a77;
                  font-size:10px;font-family:ui-monospace,Menlo,monospace}
                .dots::after{content:"";display:inline-block;width:1em;
                  text-align:left;animation:dots 1.2s steps(4,end) infinite}
                @keyframes dots{
                  0%{content:""}25%{content:"."}
                  50%{content:".."}75%{content:"..."}100%{content:""}
                }
              </style>
            </head><body><div>
              <h1>Waking up<span class="dots"></span></h1>
              <p>Render free tier naps after 15 min of no traffic.<br>
                 First request after a nap takes 30–60 s. Retrying
                 automatically…</p>
              <p class="hint">Swipe down to retry now.</p>
            </div>
            <div class="ver">build ${BuildConfig.GIT_SHA} · ${BuildConfig.BUILD_TIME}</div>
            </body></html>
        """.trimIndent()
    }
}
