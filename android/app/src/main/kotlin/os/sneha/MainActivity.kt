package os.sneha

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
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
import androidx.core.view.WindowCompat
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, true)
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

        configureWebView(webView)
        refreshLayout.setOnRefreshListener {
            // Force-refresh triggers the backend's ?force=1 path which
            // runs a fresh Oura/Garmin sync before returning HTML.
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
            }

            override fun onReceivedError(
                view: WebView, request: WebResourceRequest, error: WebResourceError
            ) {
                if (!request.isForMainFrame) return
                refreshLayout.isRefreshing = false
                // Render a clean "offline" placeholder instead of the ugly
                // system-default white error page.
                view.loadDataWithBaseURL(
                    BuildConfig.BASE_URL,
                    OFFLINE_HTML,
                    "text/html", "utf-8", null
                )
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

        // Inline, no-network fallback shown when the WebView can't reach
        // the backend (e.g. cold-start + no internet). Intentionally
        // matches the dashboard's dark palette so the app never shows
        // a jarring white page.
        private val OFFLINE_HTML = """
            <!doctype html><html><head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <style>
                html,body{height:100%;margin:0;background:#0d1b2e;color:#e8eef5;
                  font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
                  display:flex;align-items:center;justify-content:center;
                  text-align:center;padding:24px}
                h1{color:#6ee7b7;font-weight:500;letter-spacing:-0.01em;margin:0 0 10px}
                p{color:#7a9ab8;font-size:14px;line-height:1.5}
                .hint{color:#3d5a77;font-size:12px;margin-top:24px}
              </style>
            </head><body><div>
              <h1>Waking up…</h1>
              <p>Render free tier naps after 15 min of no traffic.<br>
                 First request after a nap takes 30–60 s.</p>
              <p class="hint">Swipe down to retry.</p>
            </div></body></html>
        """.trimIndent()
    }
}
