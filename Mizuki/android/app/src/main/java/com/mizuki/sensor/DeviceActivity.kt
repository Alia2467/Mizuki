package com.mizuki.sensor

import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.gson.Gson
import com.google.gson.JsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** 设备页：展示手机状态 + 电脑状态，支持下拉刷新。 */
class DeviceActivity : AppCompatActivity() {

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .build()
    private val gson = Gson()
    private val executor = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_device)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        val swipe = findViewById<SwipeRefreshLayout>(R.id.swipeRefresh)
        swipe.setOnRefreshListener {
            SensorService.requestRefresh()
            handler.postDelayed({
                refreshPhone()
                fetchComputerStatus()
                swipe.isRefreshing = false
            }, 2000)
        }

        refreshPhone()
        fetchComputerStatus()
    }

    private fun refreshPhone() {
        val data = SensorService.latestData
        findViewById<TextView>(R.id.devicePhone).text = (data?.get("device_id") ?: "未知").toString()
        findViewById<TextView>(R.id.devicePhoneStatus).text = if (data != null) "运行中" else "未运行"
        val usage = data?.get("usage") as? Map<*, *>
        findViewById<TextView>(R.id.devicePhoneApp).text = (usage?.get("foreground_app") ?: "未知").toString()
    }

    private fun fetchComputerStatus() {
        val prefs = getSharedPreferences("mizuki", Context.MODE_PRIVATE)
        val ip = prefs.getString("ip", SensorService.DEFAULT_IP)
        val port = prefs.getString("port", SensorService.DEFAULT_PORT.toString())
        val token = prefs.getString("token", "") ?: ""
        executor.execute {
            try {
                val request = Request.Builder().url("http://$ip:$port/merged-data").apply {
                    // 控制台启用鉴权后，拉取合并数据同样需要携带令牌
                    if (token.isNotEmpty()) addHeader("X-Sensor-Token", token)
                }.build()
                client.newCall(request).execute().use { resp ->
                    if (resp.isSuccessful) {
                        val body = resp.body?.string()
                        val computer = body?.let {
                            gson.fromJson(it, JsonObject::class.java).getAsJsonObject("computer")
                        }
                        val window = computer?.get("foreground_window")?.asString ?: "未知"
                        val process = computer?.get("foreground_process")?.asString ?: "未知"
                        runOnUiThread {
                            findViewById<TextView>(R.id.deviceComputerWindow).text = window
                            findViewById<TextView>(R.id.deviceComputerProcess).text = process
                        }
                    } else {
                        runOnUiThread { setComputerFailed() }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { setComputerFailed() }
            }
        }
    }

    private fun setComputerFailed() {
        findViewById<TextView>(R.id.deviceComputerWindow).text = "连接失败"
        findViewById<TextView>(R.id.deviceComputerProcess).text = "连接失败"
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdown()
        handler.removeCallbacksAndMessages(null)
    }
}
