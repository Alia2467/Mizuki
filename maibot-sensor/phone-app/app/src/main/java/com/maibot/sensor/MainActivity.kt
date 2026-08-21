package com.maibot.sensor

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
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import android.net.Uri
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog

/** 主页：首页 / 状态 / 设置三页签 + 侧边栏；连接配置、权限引导与实时状态展示。 */
class MainActivity : AppCompatActivity() {

    private lateinit var drawerLayout: DrawerLayout
    private lateinit var pageHome: View
    private lateinit var pageStatus: View
    private lateinit var pageSettings: View
    private lateinit var tabHome: TextView
    private lateinit var tabStatus: TextView
    private lateinit var tabSettings: TextView
    private lateinit var statusBox: TextView
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

    private val prefs by lazy { getSharedPreferences("maibot", Context.MODE_PRIVATE) }
    private val statusHandler = Handler(Looper.getMainLooper())

    private val healthPermissions = setOf(
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(HeartRateRecord::class),
        HealthPermission.getReadPermission(SleepSessionRecord::class),
    )

    private val healthPermissionLauncher = registerForActivityResult(
        PermissionController.createRequestPermissionResultContract()
    ) {
        // 授权流程走完就记一个标记，之后不再反复弹
        prefs.edit().putBoolean("health_perm_asked", true).apply()
        startCollectFromEdits()
    }

    // 首页横幅：从相册选图
    private val bannerPicker = registerForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri ->
        if (uri != null) {
            try {
                contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            } catch (_: Exception) {}
            prefs.edit().putString("banner_uri", uri.toString()).apply()
            loadBannerImage()
        }
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

        drawerLayout = findViewById(R.id.drawer_layout)

        val drawer = findViewById<View>(R.id.drawer)
        val drawerLp = drawer.layoutParams as DrawerLayout.LayoutParams
        drawerLp.width = resources.displayMetrics.widthPixels * 30 / 100
        drawer.layoutParams = drawerLp

        pageHome = findViewById(R.id.page_home)
        pageStatus = findViewById(R.id.page_status)
        pageSettings = findViewById(R.id.page_settings)
        tabHome = findViewById(R.id.tab_home)
        tabStatus = findViewById(R.id.tab_status)
        tabSettings = findViewById(R.id.tab_settings)
        statusBox = findViewById(R.id.statusBox)
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
        // page_status 的根是 SwipeRefreshLayout，被 include 的 id 覆盖成了 page_status
        swipeRefresh = findViewById(R.id.page_status) as SwipeRefreshLayout

        loadPrefs()
        applySavedLanguage()

        findViewById<com.google.android.material.imageview.ShapeableImageView>(R.id.bannerImage)
            .setOnClickListener {
                bannerPicker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
            }
        loadBannerImage()
        setupBrandText()

        findViewById<View>(R.id.menuButton).setOnClickListener {
            drawerLayout.openDrawer(GravityCompat.START)
        }

        val searchBox = findViewById<EditText>(R.id.searchBox)
        searchBox.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH) {
                val query = searchBox.text.toString().trim()
                startActivity(Intent(this, SearchActivity::class.java).putExtra("query", query))
                true
            } else {
                false
            }
        }

        findViewById<View>(R.id.shareButton).setOnClickListener {
            val share = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_TEXT, "maibot感知 - 手机端数据采集 APP")
            }
            startActivity(Intent.createChooser(share, "分享"))
        }

        findViewById<View>(R.id.menu_guide).setOnClickListener {
            drawerLayout.closeDrawer(GravityCompat.START)
            startActivity(Intent(this, GuideActivity::class.java))
        }
        findViewById<View>(R.id.menu_about).setOnClickListener {
            drawerLayout.closeDrawer(GravityCompat.START)
            startActivity(Intent(this, AboutActivity::class.java))
        }

        findViewById<View>(R.id.menu_note).setOnClickListener {
            drawerLayout.closeDrawer(GravityCompat.START)
            startActivity(Intent(this, NoteActivity::class.java))
        }
        findViewById<View>(R.id.menu_personalize).setOnClickListener {
            drawerLayout.closeDrawer(GravityCompat.START)
            startActivity(Intent(this, PersonalizeActivity::class.java))
        }

        findViewById<View>(R.id.themeToggle).setOnClickListener {
            val night = AppCompatDelegate.getDefaultNightMode() == AppCompatDelegate.MODE_NIGHT_YES
            AppCompatDelegate.setDefaultNightMode(
                if (night) AppCompatDelegate.MODE_NIGHT_NO else AppCompatDelegate.MODE_NIGHT_YES
            )
            drawerLayout.closeDrawer(GravityCompat.START)
        }

        findViewById<View>(R.id.startButton).setOnClickListener { onStartConnect() }
        findViewById<View>(R.id.disconnectButton).setOnClickListener { onDisconnect() }

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

        findViewById<View>(R.id.languageHeader).setOnClickListener {
            val group = findViewById<View>(R.id.languageGroup)
            group.visibility = if (group.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        }
        findViewById<View>(R.id.langZh).setOnClickListener { setLanguage("zh") }
        findViewById<View>(R.id.langEn).setOnClickListener { setLanguage("en") }
        findViewById<View>(R.id.langJa).setOnClickListener { setLanguage("ja") }

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
                applyStatusBox()
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
        // 从个性设置等页面返回后，立即重新套用字体、主题色与连接状态框
        FontManager.applyTo(this)
        applyThemeToButtons()
        applyStatusBox()
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
        // 已放弃从 Health Connect 读心率/睡眠（手表无法同步到 Health Connect），
        // 步数走手机计步传感器，连接时不再弹健康授权。
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
        applyStatusBox()
    }

    private fun applyStatusBox() {
        val connected = prefs.getBoolean("service_running", false)
        if (connected) {
            statusBox.text = getString(R.string.status_connected)
            statusBox.background = ThemeUtils.buttonDrawable(this)
            statusBox.setTextColor(ThemeUtils.contrastTextColor(ThemeUtils.accentColor(this)))
        } else {
            statusBox.text = getString(R.string.status_not_connected)
            statusBox.setBackgroundResource(R.drawable.bg_box)
            statusBox.setTextColor(ContextCompat.getColor(this, R.color.text_primary))
        }
    }

    private fun applySavedLanguage() {
        val lang = prefs.getString("lang", "zh") ?: "zh"
        val locale = when (lang) {
            "en" -> java.util.Locale.ENGLISH
            "ja" -> java.util.Locale.JAPANESE
            else -> java.util.Locale.SIMPLIFIED_CHINESE
        }
        AppCompatDelegate.setApplicationLocales(androidx.core.os.LocaleListCompat.create(locale))
    }

    private fun setLanguage(code: String) {
        prefs.edit().putString("lang", code).apply()
        val locale = when (code) {
            "en" -> java.util.Locale.ENGLISH
            "ja" -> java.util.Locale.JAPANESE
            else -> java.util.Locale.SIMPLIFIED_CHINESE
        }
        AppCompatDelegate.setApplicationLocales(androidx.core.os.LocaleListCompat.create(locale))
    }

    /** 主题色只作用于按键：首页按钮 + 设置页操作/语言按钮 + 侧边栏相框。 */
    private fun applyThemeToButtons() {
        ThemeUtils.styleButtons(
            this,
            findViewById(R.id.startButton),
            findViewById(R.id.disconnectButton),
            findViewById(R.id.locationButton),
            findViewById(R.id.weatherButton),
            findViewById(R.id.deviceButton),
            findViewById(R.id.usageAccessButton),
            findViewById(R.id.healthSyncButton),
            findViewById(R.id.langZh),
            findViewById(R.id.langEn),
            findViewById(R.id.langJa)
        )
        findViewById<View>(R.id.sidebarFrame).background = ThemeUtils.buttonDrawable(this)
    }

    private fun loadBannerImage() {
        val uriStr = prefs.getString("banner_uri", null) ?: return
        try {
            findViewById<com.google.android.material.imageview.ShapeableImageView>(R.id.bannerImage)
                .setImageURI(Uri.parse(uriStr))
        } catch (_: Exception) {}
    }

    private fun setupBrandText() {
        val brand = findViewById<TextView>(R.id.brandText)
        brand.text = prefs.getString("brand_text", getString(R.string.brand_name))
        brand.setOnClickListener { showBrandEditDialog(brand) }
    }

    private fun showBrandEditDialog(brand: TextView) {
        val input = EditText(this)
        input.setText(brand.text)
        input.setSelection(input.text.length)
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.brand_name))
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                val text = input.text.toString().trim().ifEmpty { getString(R.string.brand_name) }
                prefs.edit().putString("brand_text", text).apply()
                brand.text = text
            }
            .setNegativeButton("取消", null)
            .show()
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
