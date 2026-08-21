package com.maibot.sensor

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/** 内置天气页：当前天气 + 7 天预报，支持下拉刷新。 */
class WeatherActivity : AppCompatActivity() {

    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_weather)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        val swipe = findViewById<SwipeRefreshLayout>(R.id.swipeRefresh)
        swipe.setOnRefreshListener {
            SensorService.requestRefresh()
            handler.postDelayed({
                refreshDisplay()
                swipe.isRefreshing = false
            }, 2000)
        }

        refreshDisplay()
    }

    private fun refreshDisplay() {
        val data = SensorService.latestData
        val location = data?.get("location") as? Map<*, *>
        val weather = data?.get("weather") as? Map<*, *>

        val city = (location?.get("city") ?: "未知").toString()
        val condition = (weather?.get("condition") ?: "unknown").toString()
        val temp = weather?.get("temperature") ?: 0
        val humidity = weather?.get("humidity") ?: 0

        findViewById<TextView>(R.id.weatherCity).text = city
        findViewById<TextView>(R.id.weatherTemp).text =
            if (condition != "unknown") "${temp}°" else "—"
        findViewById<TextView>(R.id.weatherCondition).text = conditionEmoji(condition) + " " + conditionCn(condition)
        findViewById<TextView>(R.id.weatherHumidity).text =
            if (condition != "unknown") "湿度 $humidity%" else "暂无数据"

        refreshForecast()
    }

    private fun refreshForecast() {
        val container = findViewById<LinearLayout>(R.id.forecastContainer)
        container.removeAllViews()
        val forecast = SensorService.dailyForecast ?: return
        for ((index, day) in forecast.withIndex()) {
            val item = layoutInflater.inflate(R.layout.item_forecast_day, container, false)
            val date = (day["date"] ?: "").toString()
            val code = (day["code"] as? Number)?.toInt() ?: 0
            val max = (day["max"] as? Number)?.toInt() ?: 0
            val min = (day["min"] as? Number)?.toInt() ?: 0
            item.findViewById<TextView>(R.id.dayLabel).text = dayLabel(date, index)
            item.findViewById<TextView>(R.id.dayEmoji).text = codeToEmoji(code)
            item.findViewById<TextView>(R.id.dayMax).text = "$max°"
            item.findViewById<TextView>(R.id.dayMin).text = "$min°"
            container.addView(item)
        }
    }

    private fun codeToEmoji(code: Int): String {
        return when (code) {
            0 -> "☀️"
            1, 2, 3 -> "⛅"
            45, 48 -> "🌫️"
            in 51..57 -> "🌦️"
            in 61..67 -> "🌧️"
            in 71..77 -> "❄️"
            in 80..82 -> "🌧️"
            85, 86 -> "❄️"
            in 95..99 -> "⛈️"
            else -> "·"
        }
    }

    private fun dayLabel(date: String, index: Int): String {
        return when (index) {
            0 -> "今天"
            1 -> "明天"
            2 -> "后天"
            else -> {
                try {
                    val d = java.time.LocalDate.parse(date)
                    val week = arrayOf("周一", "周二", "周三", "周四", "周五", "周六", "周日")
                    week[d.dayOfWeek.value - 1]
                } catch (e: Exception) {
                    date.substringAfterLast("-")
                }
            }
        }
    }

    private fun conditionEmoji(c: String): String {
        return when (c) {
            "clear" -> "☀️"
            "cloudy" -> "⛅"
            "fog" -> "🌫️"
            "drizzle" -> "🌦️"
            "rain" -> "🌧️"
            "snow" -> "❄️"
            "shower" -> "🌧️"
            "thunderstorm" -> "⛈️"
            else -> ""
        }
    }

    private fun conditionCn(c: String): String {
        return when (c) {
            "clear" -> "晴"
            "cloudy" -> "多云"
            "fog" -> "雾"
            "drizzle" -> "毛毛雨"
            "rain" -> "雨"
            "snow" -> "雪"
            "shower" -> "阵雨"
            "thunderstorm" -> "雷暴"
            else -> "暂无"
        }
    }
}
