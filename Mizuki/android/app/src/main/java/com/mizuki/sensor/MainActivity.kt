package com.mizuki.sensor

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.appcompat.widget.SwitchCompat
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/** 主页：首页 / 状态 / 设置三页签；连接配置、权限引导与实时状态展示。 */
class MainActivity : AppCompatActivity() {

    private lateinit var pageHome: View
    private lateinit var pageStatus: View
    private lateinit var pageSettings: View
    private lateinit var tabHome: TextView
    private lateinit var tabStatus: TextView
    private lateinit var tabSettings: TextView
    private lateinit var statusLight: View
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var ipEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var intervalEdit: EditText
    private lateinit var tokenEdit: EditText

    // 状态页控件
    private lateinit var statusLocation: TextView
    private lateinit var statusWeather: TextView
    private lateinit var statusSteps: TextView
    private lateinit var statusHeart: TextView
    private lateinit var statusSleep: TextView
    private lateinit var statusMusic: TextView
    private lateinit var statusApp: TextView
    private lateinit var swipeRefresh: SwipeRefreshLayout

    private val prefs by lazy { getSharedPreferences("mizuki", Context.MODE_PRIVATE) }
    private val statusHandler = Handler(Looper.getMainLooper())

    private val healthPermissions = setOf(
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(HeartRateRecord::class),
        HealthPermission.getReadPermission(SleepSessionRecord::class),
    )

    private val healthPermissionLauncher = registerForActivityResult(
        PermissionController.createRequestPermissionResultContract()
    ) {
        prefs.edit().putBoolean("health_perm_asked", true).apply()
        startCollectFromEdits()
    }

    private val statusRunnable = object : Runnable {
        override fun run() {
            updateStatusPage()
            statusHandler.postDelayed(this, 3000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        pageHome = findViewById(R.id.page_home)
        pageStatus = findViewById(R.id.page_status)
        pageSettings = findViewById(R.id.page_settings)
        tabHome = findViewById(R.id.tab_home)
        tabStatus = findViewById(R.id.tab_status)
        tabSettings = findViewById(R.id.tab_settings)
        statusLight = findViewById(R.id.statusLight)
        statusDot = findViewById(R.id.statusDot)
        statusText = findViewById(R.id.statusText)
        ipEdit = findViewById(R.id.ipEdit)
        portEdit = findViewById(R.id.portEdit)
        intervalEdit = findViewById(R.id.intervalEdit)
        tokenEdit = findViewById(R.id.tokenEdit)

        statusLocation = findViewById(R.id.statusLocation)
        statusWeather = findViewById(R.id.statusWeather)
        statusSteps = findViewById(R.id.statusSteps)
        statusHeart = findViewById(R.id.statusHeart)
        statusSleep = findViewById(R.id.statusSleep)
        statusMusic = findViewById(R.id.statusMusic)
        statusApp = findViewById(R.id.statusApp)
        swipeRefresh = findViewById(R.id.page_status) as SwipeRefreshLayout

        loadPrefs()

        // 夜间模式开关
        val themeSwitch = findViewById<SwitchCompat>(R.id.themeSwitch)
        themeSwitch.isChecked = AppCompatDelegate.getDefaultNightMode() == AppCompatDelegate.MODE_NIGHT_YES
        themeSwitch.setOnCheckedChangeListener { _, isChecked ->
            AppCompatDelegate.setDefaultNightMode(
                if (isChecked) AppCompatDelegate.MODE_NIGHT_YES else AppCompatDelegate.MODE_NIGHT_NO
            )
        }

        // 状态灯：点击切换连接
        statusLight.setOnClickListener {
            val connected = prefs.getBoolean("service_running", false)
            if (connected) {
                onDisconnect()
            } else {
                onStartConnect()
            }
        }

        findViewById<View>(R.id.locationButton).setOnClickListener { openMap() }
        findViewById<View>(R.id.weatherButton).setOnClickListener {
            startActivity(Intent(this, WeatherActivity::class.java))
        }
        findViewById<View>(R.id.deviceButton).setOnClickListener {
            startActivity(Intent(this, DeviceActivity::class.java))
        }

        tabHome.setOnClickListener { switchPage(0) }
        tabStatus.setOnClickListener { switchPage(1) }
        tabSettings.setOnClickListener { switchPage(2) }

        findViewById<View>(R.id.connectHeader).setOnClickListener {
            val group = findViewById<View>(R.id.connectGroup)
            group.visibility = if (group.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        }

        findViewById<View>(R.id.permissionHeader).setOnClickListener {
            val group = findViewById<View>(R.id.permissionGroup)
            group.visibility = if (group.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        }

        findViewById<View>(R.id.usageAccessButton).setOnClickListener { openUsageAccessSettings() }
        findViewById<View>(R.id.healthSyncButton).setOnClickListener { requestHealthSync() }

        // 主题色：只作用于按键
        applyThemeToButtons()

        swipeRefresh.setOnRefreshListener {
            SensorService.requestRefresh()
            statusHandler.postDelayed({
                updateStatusPage()
                swipeRefresh.isRefreshing = false
            }, 2000)
        }

        val homeRefresh = pageHome as SwipeRefreshLayout
        homeRefresh.setOnRefreshListener {
            SensorService.requestRefresh()
            statusHandler.postDelayed({
                updateStatusPage()
                applyStatusLight()
                homeRefresh.isRefreshing = false
            }, 1500)
        }

        switchPage(0)
        statusHandler.post(statusRunnable)
    }

    override fun onDestroy() {
        super.onDestroy()
        statusHandler.removeCallbacks(statusRunnable)
    }

    override fun onResume() {
        super.onResume()
        FontManager.applyTo(this)
        applyThemeToButtons()
        applyStatusLight()
    }

    private fun loadPrefs() {
        ipEdit.setText(prefs.getString("ip", SensorService.DEFAULT_IP))
        portEdit.setText(prefs.getString("port", SensorService.DEFAULT_PORT.toString()))
        intervalEdit.setText(prefs.getString("interval", SensorService.DEFAULT_INTERVAL.toString()))
        tokenEdit.setText(prefs.getString("token", ""))
    }

    private fun savePrefs() {
        prefs.edit()
            .putString("ip", ipEdit.text.toString().trim())
            .putString("port", portEdit.text.toString().trim())
            .putString("interval", intervalEdit.text.toString().trim())
            .putString("token", tokenEdit.text.toString().trim())
            .apply()
    }

    private fun switchPage(index: Int) {
        pageHome.visibility = if (index == 0) View.VISIBLE else View.GONE
        pageStatus.visibility = if (index == 1) View.VISIBLE else View.GONE
        pageSettings.visibility = if (index == 2) View.VISIBLE else View.GONE
        val primary = ContextCompat.getColor(this, R.color.text_primary)
        val secondary = ContextCompat.getColor(this, R.color.text_secondary)
        tabHome.setTextColor(if (index == 0) primary else secondary)
        tabStatus.setTextColor(if (index == 1) primary else secondary)
        tabSettings.setTextColor(if (index == 2) primary else secondary)
    }

    private fun onStartConnect() {
        val ip = ipEdit.text.toString().trim()
        if (ip.isEmpty()) {
            Toast.makeText(this, "请先在设置里填写电脑 IP 地址", Toast.LENGTH_SHORT).show()
            switchPage(2)
            return
        }
        savePrefs()

        val missing = missingPermissions()
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), REQ_PERMISSIONS)
            return
        }
        requestHealthPermissions()
    }

    private fun onDisconnect() {
        stopService(Intent(this, SensorService::class.java))
        setStatus(false)
    }

    private fun requestHealthPermissions() {
        startCollectFromEdits()
    }

    private fun startCollectFromEdits() {
        val ip = ipEdit.text.toString().trim()
        val port = portEdit.text.toString().trim().toIntOrNull() ?: SensorService.DEFAULT_PORT
        val interval = intervalEdit.text.toString().trim().toIntOrNull() ?: SensorService.DEFAULT_INTERVAL
        val token = tokenEdit.text.toString().trim()
        if (ip.isNotEmpty()) startCollect(ip, port, interval, token)
    }

    private fun startCollect(ip: String, port: Int, interval: Int, token: String) {
        val intent = Intent(this, SensorService::class.java).apply {
            putExtra(SensorService.EXTRA_IP, ip)
            putExtra(SensorService.EXTRA_PORT, port)
            putExtra(SensorService.EXTRA_INTERVAL, interval)
            putExtra(SensorService.EXTRA_TOKEN, token)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        setStatus(true)
    }

    private fun setStatus(connected: Boolean) {
        prefs.edit().putBoolean("service_running", connected).apply()
        applyStatusLight()
    }

    private fun applyStatusLight() {
        val connected = prefs.getBoolean("service_running", false)
        if (connected) {
            statusDot.setBackgroundResource(R.drawable.status_dot_on)
            statusText.text = getString(R.string.status_connected)
        } else {
            statusDot.setBackgroundResource(R.drawable.status_dot_off)
            statusText.text = getString(R.string.status_not_connected)
        }
    }

    /** 主题色只作用于按键。 */
    private fun applyThemeToButtons() {
        ThemeUtils.styleButtons(
            this,
            findViewById(R.id.locationButton),
            findViewById(R.id.weatherButton),
            findViewById(R.id.deviceButton),
            findViewById(R.id.usageAccessButton),
            findViewById(R.id.healthSyncButton)
        )
    }

    private fun openUsageAccessSettings() {
        try {
            startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
        } catch (e: Exception) {
            Toast.makeText(this, "无法打开使用情况访问设置", Toast.LENGTH_SHORT).show()
        }
    }

    private fun requestHealthSync() {
        try {
            healthPermissionLauncher.launch(healthPermissions)
        } catch (e: Exception) {
            Toast.makeText(this, "Health Connect 不可用", Toast.LENGTH_SHORT).show()
        }
    }

    private fun openMap() {
        val data = SensorService.latestData
        val location = data?.get("location") as? Map<*, *>
        val lat = (location?.get("latitude") as? Number)?.toDouble() ?: 0.0
        val lng = (location?.get("longitude") as? Number)?.toDouble() ?: 0.0
        val intent = Intent(this, MapActivity::class.java).apply {
            putExtra("lat", lat)
            putExtra("lng", lng)
        }
        startActivity(intent)
    }

    private fun missingPermissions(): List<String> {
        val result = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            result.add(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_PHONE_STATE)
            != PackageManager.PERMISSION_GRANTED
        ) {
            result.add(Manifest.permission.READ_PHONE_STATE)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            result.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        return result
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_PERMISSIONS) {
            requestHealthPermissions()
        }
    }

    private fun updateStatusPage() {
        val data = SensorService.latestData ?: return
        val location = data["location"] as? Map<*, *>
        val weather = data["weather"] as? Map<*, *>
        val health = data["health"] as? Map<*, *>
        val usage = data["usage"] as? Map<*, *>

        val city = location?.get("city") ?: "未知"
        val lat = location?.get("latitude") ?: 0.0
        val lng = location?.get("longitude") ?: 0.0
        statusLocation.text = if (lat != 0.0) "$city（$lat, $lng）" else city.toString()

        val condition = weather?.get("condition") ?: "unknown"
        val temp = weather?.get("temperature") ?: 0
        val humidity = weather?.get("humidity") ?: 0
        statusWeather.text = if (condition != "unknown") "$condition · ${temp}℃ · 湿度${humidity}%" else "暂无"

        val steps = health?.get("steps") ?: 0
        statusSteps.text = if (steps != 0) "$steps 步" else "—"

        val heart = health?.get("heart_rate") ?: 0
        statusHeart.text = if (heart != 0) "$heart bpm" else getString(R.string.need_watch_data)

        val sleep = health?.get("sleep_hours") ?: 0.0
        statusSleep.text = if (sleep != 0.0) "$sleep 小时" else getString(R.string.need_watch_data)

        val musicApp = usage?.get("music_app") ?: ""
        statusMusic.text = if (musicApp.toString().isNotEmpty()) "$musicApp" else getString(R.string.not_listening)

        statusApp.text = (usage?.get("foreground_app") ?: "未知").toString()
    }

    companion object {
        private const val REQ_PERMISSIONS = 100
    }
}