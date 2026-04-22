package os.sneha.data

import com.squareup.moshi.Moshi
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Minimal HTTP client. Intentionally OkHttp + Moshi rather than
 * Retrofit — we only call two endpoints and the widget runs in a very
 * constrained context (Glance background thread), so keeping
 * dependencies tiny matters.
 */
class SnehaApi(
    private val baseUrl: String,
    timeoutSeconds: Long = 90L,
) {
    // 90 s timeout accounts for Render free-tier cold starts (~30-60 s).
    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(timeoutSeconds, TimeUnit.SECONDS)
        .readTimeout(timeoutSeconds, TimeUnit.SECONDS)
        .callTimeout(timeoutSeconds, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val moshi: Moshi = Moshi.Builder().build()
    private val todayAdapter = moshi.adapter(TodayDto::class.java)

    /**
     * Fetch `/api/today`. Pass `force=true` to append `?force=1` and
     * bypass the server's 60-second live-data cache — the widget
     * always wants the freshest possible snapshot since it only runs
     * every ~15 minutes.
     */
    fun fetchToday(force: Boolean = false): Result<TodayDto> = runCatching {
        val url = if (force) "$baseUrl/api/today?force=1" else "$baseUrl/api/today"
        val req = Request.Builder()
            .url(url)
            .header("User-Agent", "Sneha.OS-android/0.1")
            .build()
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                error("HTTP ${resp.code} from /api/today")
            }
            val body = resp.body?.string().orEmpty()
            todayAdapter.fromJson(body) ?: error("empty /api/today body")
        }
    }
}
