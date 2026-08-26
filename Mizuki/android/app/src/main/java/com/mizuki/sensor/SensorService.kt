package com.mizuki.sensor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.BitmapFactory
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Geocoder
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.gson.Gson
import com.google.gson.JsonObject
import kotlinx.coroutines.runBlocking
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.text.SimpleDateFormat
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Mizuki — 后台采集服务（前台服务）
 *
 * 采集：GPS/城市、天气（Open-Meteo）、健康（Health Connect + 计步传感器）、
 * 前台应用、导航/通话/听音乐状态，并 HTTP POST 到电脑端。
 */
class SensorService : Service(), SensorEventListener {

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var sensorManager: SensorManager
    private var stepCounter: Sensor? = null
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()
    private val gson = Gson()
    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()

    private var targetUrl = ""
    private var authToken = ""
    private var intervalMs = 300
    private var baseIntervalMs = 300  // 用户配置的基准间隔（退避恢复后回到此值）
    private var backoffMultiplier = 1  // 指数退避倍数（1 = 正常间隔）
    private var stepBaseline = -1L
    private var lastSteps = 0L

    // 天气缓存 + 最近一次有效位置（室内 GPS 失效时兜底）
    private var cachedWeather: Triple<String, Int, Int>? = null
    private var cachedWeatherTime = 0L
    private var lastLat: Double? = null
    private var lastLng: Double? = null
    private var lastCity = "未知"

    // 自诊断状态
    private var startTimeMillis = 0L
    private var sendSuccess = 0
    private var sendFailed = 0
    private var lastError = ""

    private val prefs by lazy { getSharedPreferences("mizuki_steps", Context.MODE_PRIVATE) }

    /** 离线补传队列：发送失败的载荷暂存本地，恢复后自动补传。 */
    private val pendingStore by lazy { PendingStore(applicationContext) }

    /** Health Connect 客户端缓存（懒初始化，避免每次 fetchHealth 重复创建）。 */
    private var healthConnectClient: HealthConnectClient? = null

    /** 导航/音乐 App 包名集合（从资源加载，可配置）。 */
    private var navPackages: Set<String> = emptySet()
    private var musicPackages: Set<String> = emptySet()

    private val collectRunnable = object : Runnable {
        override fun run() {
            collectAndSend()
            // 指数退避：连续失败时拉长间隔，上限 MAX_BACKOFF_MULTIPLIER
            val nextInterval = baseIntervalMs * backoffMultiplier
            handler.postDelayed(this, nextInterval)
        }
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        startTimeMillis = System.currentTimeMillis()
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        stepCounter = sensorManager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)
        stepCounter?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
        startForeground()
        // 从资源加载包名集合（可配置）
        navPackages = resources.getStringArray(R.array.nav_packages).toSet()
        musicPackages = resources.getStringArray(R.array.music_packages).toSet()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val ip = intent?.getStringExtra(EXTRA_IP) ?: DEFAULT_IP
        val port = intent?.getIntExtra(EXTRA_PORT, DEFAULT_PORT) ?: DEFAULT_PORT
        intervalMs = (intent?.getIntExtra(EXTRA_INTERVAL, DEFAULT_INTERVAL) ?: DEFAULT_INTERVAL).coerceAtLeast(100)
        baseIntervalMs = intervalMs
        backoffMultiplier = 1
        authToken = intent?.getStringExtra(EXTRA_TOKEN) ?: ""
        targetUrl = "http://$ip:$port/phone-data"
        Log.i(TAG, "开始采集 → $targetUrl，间隔 ${intervalMs}ms")

        handler.removeCallbacks(collectRunnable)
        handler.post(collectRunnable)
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private var consecutiveFailures = 0

    override fun onDestroy() {
        handler.removeCallbacks(collectRunnable)
        stepCounter?.let { sensorManager.unregisterListener(this) }
        executor.shutdown()
        instance = null
        // 服务销毁时同步状态，确保 UI 不残留「已连接」（异常中断场景）
        getSharedPreferences("mizuki", Context.MODE_PRIVATE)
            .edit().putBoolean("service_running", false).apply()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // 前台通知
    // ------------------------------------------------------------------
    private fun startForeground() {
        val channelId = "mizuki_sensor"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId,
                "海月之音",
                NotificationManager.IMPORTANCE_DEFAULT
            )
            channel.description = "海月之音正在后台采集数据"
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }

        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val largeIcon = BitmapFactory.decodeResource(resources, R.mipmap.ic_launcher)
        val notification: Notification = NotificationCompat.Builder(this, channelId)
            .setContentTitle("海月之音 运行中")
            .setContentText("正在采集并上报数据…")
            // 小图标必须用单色矢量；mipmap 彩色位图在部分系统上渲染异常
            .setSmallIcon(R.drawable.ic_notification)
            .setLargeIcon(largeIcon)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()

        // Android 14+（API 34）要求 startForeground 显式指定前台服务类型，否则抛 MissingForegroundServiceTypeException；
        // 权限未授予时抛 SecurityException，必须兜底，遵循「降级胜于中断」。
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(
                    NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION or
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "前台服务启动失败（权限未授予）: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // 采集与上报
    // ------------------------------------------------------------------
    private fun collectAndSend() {
        if (hasPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)) {
            try {
                // 低频定位策略：默认平衡功耗，仅导航中才提高精度
                val priority = if (isNavigationPackage(currentForegroundPackage()))
                    Priority.PRIORITY_HIGH_ACCURACY else Priority.PRIORITY_BALANCED_POWER_ACCURACY
                fusedLocationClient.getCurrentLocation(priority, null).addOnSuccessListener { location ->
                    // Geocoder 反解与网络请求同属阻塞 I/O，一律进采集单线程池，不占主线程
                    executor.execute {
                        var lat = location?.latitude
                        var lng = location?.longitude
                        // Fused 拿不到 → 系统 LocationManager 兜底（应对无 Google Play Services 的设备）
                        if (lat == null || lng == null) {
                            systemLastKnownLocation()?.let { (fLat, fLng) ->
                                lat = fLat
                                lng = fLng
                            }
                        }
                        // 闭包内可变局部量无法 smart cast，先固化再使用
                        val fixLat = lat
                        val fixLng = lng
                        if (fixLat != null && fixLng != null) {
                            lastLat = fixLat
                            lastLng = fixLng
                            lastCity = reverseGeocodeCity(fixLat, fixLng)
                        }
                        // GPS 拿不到就用上一次有效位置
                        val effLat = fixLat ?: lastLat
                        val effLng = fixLng ?: lastLng
                        val effCity = if (lastLat != null) lastCity else "未知"
                        buildAndSend(effLat, effLng, effCity)
                    }
                }.addOnFailureListener {
                    fallbackAndSend()
                }
            } catch (e: SecurityException) {
                fallbackAndSend()
            } catch (e: Exception) {
                // 无 GMS 设备上 Fused 客户端可能完全不可用
                Log.d(TAG, "Fused Location 不可用，改用系统定位: ${e.message}")
                fallbackAndSend()
            }
        } else {
            fallbackAndSend()
        }
    }

    /** 定位兜底链：系统 LocationManager 最近位置 → 上次有效位置 → 未知。 */
    private fun fallbackAndSend() {
        executor.execute {
            systemLastKnownLocation()?.let { (lat, lng) ->
                lastLat = lat
                lastLng = lng
                lastCity = reverseGeocodeCity(lat, lng)
            }
            buildAndSend(lastLat, lastLng, if (lastLat != null) lastCity else "未知")
        }
    }

    /** 系统 LocationManager 最近位置兜底（不依赖 Google Play Services）。 */
    private fun systemLastKnownLocation(): Pair<Double, Double>? {
        return try {
            val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
            for (provider in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
                try {
                    val loc = lm.getLastKnownLocation(provider)
                    if (loc != null) return loc.latitude to loc.longitude
                } catch (_: SecurityException) {
                } catch (_: Exception) {
                }
            }
            null
        } catch (e: Exception) {
            null
        }
    }

    private fun buildAndSend(lat: Double?, lng: Double?, city: String) {
        val foregroundApp = currentForegroundApp()
        val packageName = currentForegroundPackage()
        val weather = fetchWeather(lat, lng)   // (condition, temperature, humidity)
        val health = fetchHealth()             // (heartRate, steps, sleepHours)

        val data = mapOf(
            "device_id" to Build.MODEL,
            "timestamp" to SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault()).format(Date()),
            "location" to mapOf(
                "city" to city,
                "latitude" to (lat ?: 0.0),
                "longitude" to (lng ?: 0.0)
            ),
            "weather" to mapOf(
                "condition" to weather.first,
                "temperature" to weather.second,
                "humidity" to weather.third
            ),
            "health" to mapOf(
                "heart_rate" to health.first,
                "steps" to health.second,
                "sleep_hours" to health.third
            ),
            "usage" to mapOf(
                "foreground_app" to foregroundApp,
                "is_navigating" to isNavigationPackage(packageName),
                "is_calling" to isCalling(),
                "is_listening_music" to isMusicPackage(packageName),
                "music_app" to if (isMusicPackage(packageName)) foregroundApp else "",
                "screen_text" to ""
            ),
            "diagnostics" to buildDiagnostics()
        )

        latestData = data
        send(gson.toJson(data))
    }

    /** 4xx 属永久性拒收（请求非法/鉴权失败），重试不会成功，不入离线队列。 */
    private fun isPermanentReject(code: Int): Boolean = code in 400..499

    private fun send(json: String) {
        val body = json.toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = buildRequest(body)
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                sendFailed++
                lastError = e.message ?: "连接失败"
                Log.e(TAG, "上报失败: $lastError")
                consecutiveFailures++
                // 指数退避：连续失败时加倍间隔（上限 MAX_BACKOFF_MULTIPLIER），不停止服务
                if (backoffMultiplier < MAX_BACKOFF_MULTIPLIER) {
                    backoffMultiplier *= 2
                    Log.w(TAG, "连续失败 $consecutiveFailures 次，退避倍数 ×$backoffMultiplier（间隔 ${baseIntervalMs * backoffMultiplier}ms）")
                } else {
                    Log.e(TAG, "连续失败 $consecutiveFailures 次，已达最大退避")
                }
                pendingStore.enqueue(json)
            }

            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    sendSuccess++
                    // 成功时重置退避
                    if (consecutiveFailures > 0 || backoffMultiplier > 1) {
                        consecutiveFailures = 0
                        backoffMultiplier = 1
                        Log.i(TAG, "连接恢复，重置退避")
                    }
                    Log.d(TAG, "上报成功")
                    sendPending()
                } else {
                    sendFailed++
                    lastError = "HTTP ${response.code}"
                    Log.e(TAG, "上报失败: $lastError")
                    if (isPermanentReject(response.code)) {
                        // 永久拒收不累计失败次数（非网络问题）
                        Log.e(TAG, "永久拒收，不累计连续失败")
                    } else {
                        consecutiveFailures++
                        if (backoffMultiplier < MAX_BACKOFF_MULTIPLIER) {
                            backoffMultiplier *= 2
                            Log.w(TAG, "连续失败 $consecutiveFailures 次，退避倍数 ×$backoffMultiplier")
                        }
                    }
                    if (!isPermanentReject(response.code)) pendingStore.enqueue(json)
                }
                response.close()
            }
        })
    }

    /** 构造上报请求（配置了 token 时携带鉴权头）。 */
    private fun buildRequest(body: RequestBody): Request {
        val builder = Request.Builder().url(targetUrl).post(body)
        if (authToken.isNotEmpty()) builder.addHeader("X-Sensor-Token", authToken)
        return builder.build()
    }

    /** 补传离线队列：单线程池内同步发送，每轮最多 5 条，失败即停下轮再试。 */
    private fun sendPending() {
        executor.execute { sendPendingSync() }
    }

    private fun sendPendingSync() {
        var sent = 0
        while (sent < 5) {
            val items = pendingStore.peek(1)
            if (items.isEmpty()) break
            val (id, payload) = items.first()
            try {
                val body = payload.toRequestBody("application/json; charset=utf-8".toMediaType())
                client.newCall(buildRequest(body)).execute().use { resp ->
                    if (resp.isSuccessful) {
                        pendingStore.remove(id)
                        sent++
                    } else if (isPermanentReject(resp.code)) {
                        // 永久拒收的载荷丢弃，避免无限重试
                        pendingStore.remove(id)
                        Log.e(TAG, "补传被永久拒收: HTTP ${resp.code}，丢弃该条")
                    } else {
                        Log.e(TAG, "补传失败: HTTP ${resp.code}，下轮重试")
                        return
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "补传失败: ${e.message}，下轮重试")
                return
            }
        }
        if (sent > 0) Log.i(TAG, "离线补传成功 $sent 条")
    }

    // ------------------------------------------------------------------
    // 天气（Open-Meteo，无需 API Key）
    // ------------------------------------------------------------------
    private fun fetchWeather(lat: Double?, lng: Double?): Triple<String, Int, Int> {
        val now = System.currentTimeMillis()
        // 天气 15 分钟内用缓存，避免频繁请求
        if (cachedWeather != null && now - cachedWeatherTime < 15 * 60 * 1000L) {
            return cachedWeather!!
        }
        val result = try {
            // 有 GPS 用坐标，没有则用 IP 定位兜底
            var wLat = lat
            var wLng = lng
            if (wLat == null || wLng == null) {
                val ip = ipGeolocation()
                if (ip != null) {
                    wLat = ip.first
                    wLng = ip.second
                }
            }
            if (wLat == null || wLng == null) return Triple("unknown", 0, 0)
            val request = Request.Builder().url(
                "https://api.open-meteo.com/v1/forecast?latitude=$wLat&longitude=$wLng&current=temperature_2m,relative_humidity_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=7"
            ).build()
            client.newCall(request).execute().use { resp ->
                if (resp.isSuccessful) {
                    val body = resp.body?.string()
                    if (body != null) {
                        val json = gson.fromJson(body, JsonObject::class.java)
                        val cur = json.getAsJsonObject("current")
                        // 解析 7 天预报
                        try {
                            val daily = json.getAsJsonObject("daily")
                            val times = daily.getAsJsonArray("time")
                            val codes = daily.getAsJsonArray("weather_code")
                            val maxs = daily.getAsJsonArray("temperature_2m_max")
                            val mins = daily.getAsJsonArray("temperature_2m_min")
                            val list = mutableListOf<Map<String, Any>>()
                            for (i in 0 until times.size()) {
                                list.add(
                                    mapOf(
                                        "date" to times.get(i).asString,
                                        "code" to codes.get(i).asInt,
                                        "max" to maxs.get(i).asDouble,
                                        "min" to mins.get(i).asDouble
                                    )
                                )
                            }
                            dailyForecast = list
                        } catch (e: Exception) {
                            dailyForecast = null
                        }
                        Triple(
                            weatherCodeToCondition(cur.get("weather_code").asInt),
                            cur.get("temperature_2m").asInt,
                            cur.get("relative_humidity_2m").asInt
                        )
                    } else Triple("unknown", 0, 0)
                } else Triple("unknown", 0, 0)
            }
        } catch (e: Exception) {
            Triple("unknown", 0, 0)
        }
        if (result.first != "unknown") {
            cachedWeather = result
            cachedWeatherTime = now
        }
        return result
    }

    private fun weatherCodeToCondition(code: Int): String {
        return when (code) {
            0 -> "clear"
            1, 2, 3 -> "cloudy"
            45, 48 -> "fog"
            in 51..57 -> "drizzle"
            in 61..67 -> "rain"
            in 71..77 -> "snow"
            in 80..82 -> "shower"
            85, 86 -> "snow"
            in 95..99 -> "thunderstorm"
            else -> "unknown"
        }
    }

    /** 无 GPS 时用 IP 定位获取坐标。 */
    private fun ipGeolocation(): Pair<Double, Double>? {
        return try {
            val request = Request.Builder().url("https://ipapi.co/json/").build()
            client.newCall(request).execute().use { resp ->
                if (resp.isSuccessful) {
                    val body = resp.body?.string()
                    if (body != null) {
                        val json = gson.fromJson(body, JsonObject::class.java)
                        Pair(json.get("latitude").asDouble, json.get("longitude").asDouble)
                    } else null
                } else null
            }
        } catch (e: Exception) {
            null
        }
    }

    // ------------------------------------------------------------------
    // 健康数据（Health Connect + 计步传感器兜底）
    // ------------------------------------------------------------------
    private fun fetchHealth(): Triple<Int, Long, Double> {
        var heartRate = 0
        var steps = lastSteps
        var sleepHours = 0.0
        try {
            runBlocking {
                val hc = healthConnectClient ?: HealthConnectClient.getOrCreate(this@SensorService).also { healthConnectClient = it }
                val now = Instant.now()
                val zone = ZoneId.systemDefault()
                val startOfDay = now.atZone(zone).toLocalDate().atStartOfDay(zone).toInstant()

                // 心率（最近 6 小时内最新一条）
                val hrResp = hc.readRecords(
                    ReadRecordsRequest(
                        recordType = HeartRateRecord::class,
                        timeRangeFilter = TimeRangeFilter.between(now.minus(6, ChronoUnit.HOURS), now),
                        ascendingOrder = false,
                    )
                )
                heartRate = hrResp.records.firstOrNull()
                    ?.samples?.lastOrNull()?.beatsPerMinute?.toInt() ?: 0

                // 步数（今天累计）
                val stepsResp = hc.readRecords(
                    ReadRecordsRequest(
                        recordType = StepsRecord::class,
                        timeRangeFilter = TimeRangeFilter.between(startOfDay, now),
                    )
                )
                val hcSteps = stepsResp.records.sumOf { it.count }
                if (hcSteps > 0) steps = hcSteps

                // 睡眠（最近 24 小时）
                val sleepResp = hc.readRecords(
                    ReadRecordsRequest(
                        recordType = SleepSessionRecord::class,
                        timeRangeFilter = TimeRangeFilter.between(now.minus(24, ChronoUnit.HOURS), now),
                    )
                )
                val totalMillis = sleepResp.records.sumOf {
                    Duration.between(it.startTime, it.endTime).toMillis()
                }
                sleepHours = totalMillis / 3600000.0
            }
        } catch (e: Exception) {
            Log.d(TAG, "读取健康数据失败（用传感器步数兜底）: ${e.message}")
        }
        return Triple(heartRate, steps, sleepHours)
    }

    // ------------------------------------------------------------------
    // 单项采集
    // ------------------------------------------------------------------
    private fun currentForegroundPackage(): String {
        if (!hasUsageAccess()) return ""
        return try {
            val usm = getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val now = System.currentTimeMillis()
            val stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, now - 60_000L, now)
            stats.maxByOrNull { it.lastTimeUsed }?.packageName ?: ""
        } catch (e: Exception) {
            ""
        }
    }

    private fun currentForegroundApp(): String {
        val packageName = currentForegroundPackage()
        if (packageName.isEmpty()) return "未知"
        return try {
            val pm = packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(packageName, 0)).toString()
        } catch (e: Exception) {
            packageName
        }
    }

    private fun isNavigationPackage(packageName: String): Boolean {
        if (packageName.isEmpty()) return false
        return packageName in navPackages
    }

    private fun isMusicPackage(packageName: String): Boolean {
        if (packageName.isEmpty()) return false
        return packageName in musicPackages
    }

    private fun isCalling(): Boolean {
        // 蜂窝通话
        val cellular = try {
            val tm = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            tm.callState == TelephonyManager.CALL_STATE_OFFHOOK ||
                tm.callState == TelephonyManager.CALL_STATE_RINGING
        } catch (e: SecurityException) {
            false
        }
        // VoIP 通话（QQ/微信语音等）：音频处于通信模式
        val voip = try {
            val am = getSystemService(Context.AUDIO_SERVICE) as android.media.AudioManager
            am.mode == android.media.AudioManager.MODE_IN_COMMUNICATION
        } catch (e: Exception) {
            false
        }
        return cellular || voip
    }

    private fun reverseGeocodeCity(lat: Double, lng: Double): String {
        return try {
            val geocoder = Geocoder(this, Locale.CHINA)
            val addresses = geocoder.getFromLocation(lat, lng, 1)
            addresses?.firstOrNull()?.locality
                ?: addresses?.firstOrNull()?.adminArea
                ?: "未知"
        } catch (e: Exception) {
            "未知"
        }
    }

    private fun hasUsageAccess(): Boolean {
        return try {
            val appOps = getSystemService(Context.APP_OPS_SERVICE) as android.app.AppOpsManager
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                appOps.unsafeCheckOpNoThrow(
                    android.app.AppOpsManager.OPSTR_GET_USAGE_STATS,
                    android.os.Process.myUid(),
                    packageName
                )
            } else {
                @Suppress("DEPRECATION")
                appOps.checkOpNoThrow(
                    android.app.AppOpsManager.OPSTR_GET_USAGE_STATS,
                    android.os.Process.myUid(),
                    packageName
                )
            }
            mode == android.app.AppOpsManager.MODE_ALLOWED
        } catch (e: Exception) {
            false
        }
    }

    private fun hasPermission(permission: String): Boolean {
        return try {
            checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
        } catch (e: Exception) {
            false
        }
    }

    private fun buildDiagnostics(): Map<String, Any> {
        val location = hasPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)
        val phoneState = hasPermission(android.Manifest.permission.READ_PHONE_STATE)
        val usageAccess = hasUsageAccess()
        val notification = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            hasPermission(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            true
        }

        val warnings = mutableListOf<String>()
        if (!location) warnings.add("未授予定位权限，无法采集 GPS")
        if (!phoneState) warnings.add("未授予通话状态权限，无法判断是否在打电话")
        if (!usageAccess) warnings.add("未开启「使用情况访问」权限，无法读取前台应用")

        return mapOf(
            // 版本号唯一数据源是 build.gradle 的 versionName，不硬编码
            "app_version" to BuildConfig.VERSION_NAME,
            "running_seconds" to ((System.currentTimeMillis() - startTimeMillis) / 1000),
            "send_success" to sendSuccess,
            "send_failed" to sendFailed,
            "last_error" to lastError,
            "permissions" to mapOf(
                "location" to location,
                "phone_state" to phoneState,
                "usage_access" to usageAccess,
                "notification" to notification
            ),
            "warnings" to warnings
        )
    }

    // ------------------------------------------------------------------
    // 步数传感器回调（Health Connect 不可用时兜底）
    // ------------------------------------------------------------------
    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type != Sensor.TYPE_STEP_COUNTER) return
        val current = event.values[0].toLong()
        if (stepBaseline < 0) {
            stepBaseline = prefs.getLong("baseline", -1L)
            if (stepBaseline < 0 || current < stepBaseline) {
                stepBaseline = current
                prefs.edit().putLong("baseline", stepBaseline).apply()
            }
        }
        if (current < stepBaseline) {
            stepBaseline = current
            prefs.edit().putLong("baseline", stepBaseline).apply()
        }
        lastSteps = current - stepBaseline
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
        // 无需处理
    }

    companion object {
        const val EXTRA_IP = "extra_ip"
        const val EXTRA_PORT = "extra_port"
        const val EXTRA_INTERVAL = "extra_interval"
        const val EXTRA_TOKEN = "extra_token"

        /** 连接默认值：各页面占位/兜底的唯一数据源，禁止另处重复字面量。 */
        const val DEFAULT_IP = "192.168.1.4"
        const val DEFAULT_PORT = 821
        const val DEFAULT_INTERVAL = 300

        private const val NOTIFICATION_ID = 1
        private const val TAG = "MizukiSensor"

        /** 指数退避最大倍数：连续失败时隔隔翻倍，上限此值（默认间隔 300ms × 8 = 最长 2.4s）。 */
        private const val MAX_BACKOFF_MULTIPLIER = 8

        /** 最近一次采集到的完整数据（供状态页展示）。 */
        @Volatile
        var latestData: Map<String, Any>? = null

        /** 7 天天气预报（供天气页展示）。 */
        @Volatile
        var dailyForecast: List<Map<String, Any>>? = null

        @Volatile
        private var instance: SensorService? = null

        /** 供状态页下拉刷新时，立即触发一次采集。 */
        fun requestRefresh() {
            instance?.let { svc ->
                svc.handler.post { svc.collectAndSend() }
            }
        }
    }
}
