package os.sneha.data

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * Wire representation of `GET /api/today`. Shape is pinned server-side
 * in `app.py::api_today`; keep these in sync.
 */
@JsonClass(generateAdapter = false)
data class TodayDto(
    val ok: Boolean = true,
    val date: String = "",
    val weekday: String = "",
    val steps: Int = 0,
    @Json(name = "steps_goal") val stepsGoal: Int = 8000,
    @Json(name = "steps_left") val stepsLeft: Int = 0,
    @Json(name = "sleep_hours") val sleepHours: Double? = null,
    val calories: Int? = null,
    @Json(name = "calorie_goal") val calorieGoal: Int = 0,
    @Json(name = "cycle_phase") val cyclePhase: String = "",
    @Json(name = "cycle_day") val cycleDay: Int? = null,
    @Json(name = "morning_star") val morningStar: Boolean = false,
    @Json(name = "night_star") val nightStar: Boolean = false,
    val sauna: Boolean = false,
    @Json(name = "core_done") val coreDone: Int = 0,
    @Json(name = "core_threshold") val coreThreshold: Int = 4,
    @Json(name = "stars_today") val starsToday: Int = 0,
    @Json(name = "stars_week") val starsWeek: Int = 0,
    @Json(name = "last_sync") val lastSync: String? = null,
)
