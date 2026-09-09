package com.mizuki.sensor

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Address
import android.location.Geocoder
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.telephony.TelephonyManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import com.google.gson.Gson
import com.google.gson.JsonObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/** 实时上传、慢速采集与历史补传各自串行；同一服务运行期使用不可变连接配置和会话。 */
class SensorService : Service(), SensorEventListener {
    private val handler = Handler(Looper.getMainLooper())
    private val rootJob = SupervisorJob()
    private val scope = CoroutineScope(rootJob + Dispatchers.Main.immediate)
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "sensor-upload") }
    private val metadataExecutor = Executors.newSingleThreadExecutor { Thread(it, "sensor-metadata") }
    private val replayExecutor = Executors.newSingleThreadExecutor { Thread(it, "sensor-replay") }
    private val uploadDispatcher = executor.asCoroutineDispatcher()
    private val metadataDispatcher = metadataExecutor.asCoroutineDispatcher()
    private val replayDispatcher = replayExecutor.asCoroutineDispatcher()
    private val gate = CollectionGate()
    private val sequence = UploadSequence()
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .callTimeout(20, TimeUnit.SECONDS)
        .build()
    private val calls = SensorCalls(client::newCall)
    private val gson = Gson()
    private val prefs by lazy { getSharedPreferences("mizuki", Context.MODE_PRIVATE) }
    private val stepPrefs by lazy { getSharedPreferences("mizuki_steps", Context.MODE_PRIVATE) }
    private lateinit var config: SensorConfig
    private lateinit var pendingStore: PendingStore
    private lateinit var sensorManager: SensorManager
    private var healthConnectClient: HealthConnectClient? = null
    private var hasStarted = false
    private var backoffMultiplier = 1
    private var startElapsed = 0L
    private var sendSuccess = 0
    private var sendFailed = 0
    private var lastError = ""
    private var navPackages: Set<String> = emptySet()
    private var musicPackages: Set<String> = emptySet()

    private data class Position(val lat: Double?, val lng: Double?, val city: String = "未知")
    private data class Health(val heart: Int = 0, val steps: Long? = null, val sleep: Double = 0.0, val day: String = "")
    @Volatile private var position = Position(null, null)
    private val weather = WeatherCache(WEATHER_CACHE_MS, WEATHER_RETRY_MS)
    @Volatile private var health = Health()
    @Volatile private var stepState = StepState()
    @Volatile private var foregroundPackage = ""
    private var foregroundApp = "未知"
    private var nextForegroundCheck = 0L
    private var nextLocationCheck = 0L
    private var nextCityCheck = 0L
    @Volatile private var nextHealthCheck = 0L
    private var bootCount = -1

    private val collectRunnable = Runnable { collectAndSend() }

    override fun onCreate() {
        super.onCreate()
        // 不在 onCreate 提前注册需要运行时权限的传感器，也不在前台化成功前公布运行状态。
        pendingStore = PendingStore(applicationContext)
        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        navPackages = resources.getStringArray(R.array.nav_packages).toSet()
        musicPackages = resources.getStringArray(R.array.music_packages).toSet()
        bootCount = try { Settings.Global.getInt(contentResolver, Settings.Global.BOOT_COUNT, -1) } catch (_: Exception) { -1 }
        stepState = StepState(stepPrefs.getString("day", "") ?: "", stepPrefs.getInt("boot", -1),
            stepPrefs.getLong("counter", -1), stepPrefs.getLong("total", 0))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (gate.isStopped) { stopSelf(); return START_NOT_STICKY }
        if (hasStarted) { collectAndSend(); return START_STICKY }
        try {
            config = loadConfig(intent)
            check(startForeground()) { "请授予定位或运动识别权限后再连接" }
        } catch (e: Exception) {
            startupError = e.message ?: "采集服务启动失败"
            Log.e(TAG, startupError)
            stopCollection()
            stopSelf()
            return START_NOT_STICKY
        }
        hasStarted = true
        startElapsed = SystemClock.elapsedRealtime()
        instance = this
        isRunning = true
        isConnected = false
        startupError = ""
        prefs.edit().putBoolean("service_running", true)
            .putString("active_ip", config.host).putString("active_port", config.port.toString())
            .putString("active_interval", config.intervalMs.toString()).putString("active_token", config.token).apply()
        if (hasMotionPermission(this)) {
            try {
                sensorManager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)?.let {
                    sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL)
                }
            } catch (e: Exception) { Log.w(TAG, "计步传感器不可用", e) }
        }
        Log.i(TAG, "开始采集 → ${config.uploadUrl()}，间隔 ${config.intervalMs}ms")
        scope.launch(metadataDispatcher) { collectMetadata() }
        scope.launch(replayDispatcher) { sendPending() }
        collectAndSend()
        return START_STICKY
    }

    private fun loadConfig(intent: Intent?): SensorConfig {
        val saved = listOf("ip", "port", "interval", "token").associateWith { key ->
            prefs.getString("active_$key", prefs.getString(key, null))
        }.mapNotNull { (key, value) -> value?.let { key to it } }.toMap()
        val explicit = mutableMapOf<String, String>()
        if (intent?.hasExtra(EXTRA_IP) == true) explicit["ip"] = intent.getStringExtra(EXTRA_IP) ?: ""
        if (intent?.hasExtra(EXTRA_PORT) == true) explicit["port"] = intent.getIntExtra(EXTRA_PORT, DEFAULT_PORT).toString()
        if (intent?.hasExtra(EXTRA_INTERVAL) == true) explicit["interval"] = intent.getIntExtra(EXTRA_INTERVAL, DEFAULT_INTERVAL).toString()
        if (intent?.hasExtra(EXTRA_TOKEN) == true) explicit["token"] = intent.getStringExtra(EXTRA_TOKEN) ?: ""
        return SensorConfig.resolve(explicit, saved)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    /** 先关闭门控并取消全部请求，再取消协程/排队工作；迟到回调只能观察取消状态。 */
    private fun stopCollection() {
        if (gate.isStopped) return
        gate.stop()
        calls.stop()
        scope.cancel()
        handler.removeCallbacksAndMessages(null)
        sensorManager.unregisterListener(this)
        val cancelledTasks = executor.shutdownNow() + metadataExecutor.shutdownNow() + replayExecutor.shutdownNow()
        uploadDispatcher.close()
        metadataDispatcher.close()
        replayDispatcher.close()
        if (instance === this || instance == null) {
            instance = null
            isRunning = false
            isConnected = false
            latestData = null
            dailyForecast = null
            prefs.edit().putBoolean("service_running", false).apply()
        }
        stopForeground(STOP_FOREGROUND_REMOVE)
        // 等被取消的使用者退出后再关闭数据库；清理不阻塞主线程，也不会重新开放数据库。
        CoroutineScope(Dispatchers.IO).launch {
            // shutdownNow 排出的已取消续体仍需执行取消清理，否则其 Job 无法结束。
            cancelledTasks.forEach { task -> runCatching { task.run() } }
            rootJob.join()
            pendingStore.close()
            client.connectionPool.evictAll()
        }
    }

    override fun onDestroy() {
        stopCollection()
        super.onDestroy()
    }

    private fun startForeground(): Boolean {
        val types = foregroundTypes(this)
        if (types == 0 && Build.VERSION.SDK_INT >= 34) return false
        val channelId = "mizuki_sensor"
        val channel = NotificationChannel(channelId, "海月之音", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        val pendingIntent = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(this, channelId)
            .setContentTitle("海月之音 运行中").setContentText("正在采集并上报数据…")
            .setSmallIcon(R.drawable.ic_notification).setContentIntent(pendingIntent).setOngoing(true).build()
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, types)
        return true
    }

    /** 完成（含失败）后才调度下一次；所有刷新都走同一门控，没有无限 HTTP enqueue 队列。 */
    private fun collectAndSend() {
        if (!hasStarted || !gate.collect()) return
        handler.removeCallbacks(collectRunnable)
        scope.launch(uploadDispatcher) {
            try {
                currentCoroutineContext().ensureActive()
                buildAndSend()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                currentCoroutineContext().ensureActive()
                lastError = e.message ?: "采集失败"
                Log.e(TAG, lastError, e)
                backoffMultiplier = (backoffMultiplier * 2).coerceAtMost(MAX_BACKOFF_MULTIPLIER)
            } finally {
                gate.finish()
                if (!gate.isStopped) handler.post {
                    if (!gate.isStopped) handler.postDelayed(collectRunnable,
                        (config.intervalMs * backoffMultiplier).coerceAtMost(SensorConfig.MAX_INTERVAL_MS))
                }
            }
        }
    }

    private suspend fun buildAndSend() {
        collectForeground()
        val stamp = sequence.snapshot()
        val loc = position
        val weatherNow = weather.value
        val healthNow = health
        val appPackage = foregroundPackage
        val steps = healthNow.steps.takeIf { healthNow.day == LocalDate.now().toString() } ?: stepsToday()
        val data = mapOf(
            "device_id" to Build.MODEL,
            "timestamp" to DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss", Locale.ROOT).format(java.time.LocalDateTime.now()),
            "location" to mapOf("city" to loc.city, "latitude" to (loc.lat ?: 0.0), "longitude" to (loc.lng ?: 0.0)),
            "weather" to mapOf("condition" to weatherNow.first, "temperature" to weatherNow.second, "humidity" to weatherNow.third),
            "health" to mapOf("heart_rate" to healthNow.heart, "steps" to steps, "sleep_hours" to healthNow.sleep),
            "usage" to mapOf("foreground_app" to foregroundApp, "is_navigating" to (appPackage in navPackages),
                "is_calling" to isCalling(), "is_listening_music" to (appPackage in musicPackages),
                "music_app" to if (appPackage in musicPackages) foregroundApp else "", "screen_text" to ""),
            "diagnostics" to buildDiagnostics()
        )
        currentCoroutineContext().ensureActive()
        // 发布放回主线程，确保 stopCollection 之后旧运行期不再写 UI 的全局快照。
        handler.post { if (!gate.isStopped && instance === this) latestData = data }
        val json = gson.toJson(data)
        val request = buildUploadRequest(config, json, stamp)
        var shouldStore: Boolean
        try {
            val code = calls.fetch(request) { it.code }
            currentCoroutineContext().ensureActive()
            if (code in 200..299) {
                sendSuccess++
                lastError = ""
                backoffMultiplier = 1
                handler.post { if (!gate.isStopped && instance === this) isConnected = true }
                return
            }
            lastError = "HTTP $code"
            shouldStore = !isPermanentReject(code)
        } catch (e: IOException) {
            currentCoroutineContext().ensureActive()
            lastError = e.message ?: "连接失败"
            shouldStore = true
        }
        currentCoroutineContext().ensureActive()
        sendFailed++
        backoffMultiplier = (backoffMultiplier * 2).coerceAtMost(MAX_BACKOFF_MULTIPLIER)
        handler.post { if (!gate.isStopped && instance === this) isConnected = false }
        Log.w(TAG, "上报失败: $lastError")
        if (shouldStore && !gate.isStopped) pendingStore.enqueue(json)
    }

    /** 独立单线程，每次只补一条，完成后至少等一秒；实时上传失败时暂停补传。 */
    private suspend fun sendPending() {
        while (currentCoroutineContext().isActive) {
            delay(REPLAY_INTERVAL_MS)
            if (!isConnected || gate.isStopped) continue
            try {
                val item = pendingStore.peek(1).firstOrNull() ?: continue
                val request = buildUploadRequest(config, item.second, isReplay = true)
                val code = calls.fetch(request) { it.code }
                currentCoroutineContext().ensureActive()
                if (code in 200..299 || isPermanentReject(code)) pendingStore.remove(item.first)
                else delay(REPLAY_FAILURE_MS)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                currentCoroutineContext().ensureActive()
                Log.w(TAG, "历史补传暂缓: ${e.message}")
                delay(REPLAY_FAILURE_MS)
            }
        }
    }

    /** 外部慢数据源从不占实时上传线程；使用单调时钟做缓存/失败冷却。 */
    private suspend fun collectMetadata() {
        while (currentCoroutineContext().isActive) {
            try {
                if (SystemClock.elapsedRealtime() >= nextLocationCheck) {
                    nextLocationCheck = SystemClock.elapsedRealtime() + LOCATION_INTERVAL_MS
                    collectLocation()
                }
                if (SystemClock.elapsedRealtime() >= nextHealthCheck) {
                    nextHealthCheck = SystemClock.elapsedRealtime() + HEALTH_INTERVAL_MS
                    fetchHealth()
                }
                if (weather.collect(SystemClock.elapsedRealtime())) fetchWeather()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                currentCoroutineContext().ensureActive()
                Log.w(TAG, "慢速采集失败，保留缓存: ${e.message}")
            }
            delay(METADATA_TICK_MS)
        }
    }

    private suspend fun collectLocation() {
        val fix = if (hasLocationPermission(this)) {
            fetchFusedLocation() ?: systemLastKnownLocation() ?: fetchSystemLocation()
        } else null
        currentCoroutineContext().ensureActive()
        if (fix != null) position = position.copy(lat = fix.latitude, lng = fix.longitude)
        val loc = position
        if (loc.lat != null && loc.lng != null && SystemClock.elapsedRealtime() >= nextCityCheck) {
            nextCityCheck = SystemClock.elapsedRealtime() + CITY_INTERVAL_MS
            val city = reverseGeocodeCity(loc.lat, loc.lng)
            currentCoroutineContext().ensureActive()
            if (city != null) position = position.copy(city = city)
        }
    }

    private suspend fun fetchFusedLocation(): Location? = withTimeoutOrNull(LOCATION_TIMEOUT_MS) {
        suspendCancellableCoroutine { continuation ->
            val token = CancellationTokenSource()
            continuation.invokeOnCancellation { token.cancel() }
            try {
                val priority = if (foregroundPackage in navPackages) Priority.PRIORITY_HIGH_ACCURACY else Priority.PRIORITY_BALANCED_POWER_ACCURACY
                LocationServices.getFusedLocationProviderClient(this@SensorService)
                    .getCurrentLocation(priority, token.token)
                    .addOnSuccessListener { if (continuation.isActive) continuation.resume(it) }
                    .addOnFailureListener { if (continuation.isActive) continuation.resume(null) }
                    .addOnCanceledListener { if (continuation.isActive) continuation.resume(null) }
            } catch (e: Exception) { if (continuation.isActive) continuation.resume(null) }
        }
    }

    private fun systemLastKnownLocation(): Location? = try {
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).mapNotNull {
            try { lm.getLastKnownLocation(it) } catch (_: Exception) { null }
        }.filter { (SystemClock.elapsedRealtimeNanos() - it.elapsedRealtimeNanos) / 1000000 < LOCATION_MAX_AGE_MS }
            .maxByOrNull { it.elapsedRealtimeNanos }
    } catch (_: Exception) { null }

    private suspend fun fetchSystemLocation(): Location? = withTimeoutOrNull(LOCATION_TIMEOUT_MS) {
        suspendCancellableCoroutine { continuation ->
            val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val listener = object : LocationListener {
                override fun onLocationChanged(location: Location) {
                    try { lm.removeUpdates(this) } catch (_: Exception) {}
                    if (continuation.isActive) continuation.resume(location)
                }
                override fun onProviderEnabled(provider: String) {}
                override fun onProviderDisabled(provider: String) {}
                @Deprecated("Legacy callback")
                override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
            }
            continuation.invokeOnCancellation { try { lm.removeUpdates(listener) } catch (_: Exception) {} }
            try {
                val provider = listOf(LocationManager.NETWORK_PROVIDER, LocationManager.GPS_PROVIDER)
                    .firstOrNull { lm.isProviderEnabled(it) }
                if (provider == null) continuation.resume(null)
                else {
                    @Suppress("DEPRECATION")
                    lm.requestSingleUpdate(provider, listener, Looper.getMainLooper())
                    if (!continuation.isActive) lm.removeUpdates(listener)
                }
            } catch (_: Exception) { if (continuation.isActive) continuation.resume(null) }
        }
    }

    private suspend fun reverseGeocodeCity(lat: Double, lng: Double): String? {
        if (!Geocoder.isPresent()) return null
        return try {
            val geocoder = Geocoder(this, Locale.CHINA)
            val addresses = if (Build.VERSION.SDK_INT >= 33) {
                withTimeoutOrNull(LOCATION_TIMEOUT_MS) {
                    suspendCancellableCoroutine<List<Address>> { continuation ->
                        geocoder.getFromLocation(lat, lng, 1, object : Geocoder.GeocodeListener {
                            override fun onGeocode(addresses: MutableList<Address>) {
                                if (continuation.isActive) continuation.resume(addresses)
                            }
                            override fun onError(errorMessage: String?) {
                                if (continuation.isActive) continuation.resume(emptyList())
                            }
                        })
                    }
                }
            } else {
                @Suppress("DEPRECATION")
                geocoder.getFromLocation(lat, lng, 1)
            }
            addresses?.firstOrNull()?.let { it.locality ?: it.adminArea }
        } catch (e: CancellationException) { throw e }
        catch (_: Exception) { null }
    }

    /** 失败冷却也会更新；失败保留旧天气及预报，而不是每轮重试或写成零值。 */
    private suspend fun fetchWeather() {
        val loc = position
        val coords = if (loc.lat != null && loc.lng != null) loc.lat to loc.lng else ipGeolocation()
        currentCoroutineContext().ensureActive()
        if (coords == null) return
        if (loc.lat == null || loc.lng == null) position = loc.copy(lat = coords.first, lng = coords.second)
        val request = Request.Builder().url("https://api.open-meteo.com/v1/forecast?latitude=${coords.first}&longitude=${coords.second}&current=temperature_2m,relative_humidity_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=7&timezone=auto").build()
        val json = calls.fetch(request) { response ->
            if (!response.isSuccessful) throw IOException("天气 HTTP ${response.code}")
            gson.fromJson(response.body?.string(), JsonObject::class.java)
        } ?: return
        val cur = json.getAsJsonObject("current") ?: return
        val result = Triple(weatherCodeToCondition(cur.get("weather_code").asInt), cur.get("temperature_2m").asInt,
            cur.get("relative_humidity_2m").asInt)
        if (result.first == "unknown") return
        currentCoroutineContext().ensureActive()
        weather.record(result, SystemClock.elapsedRealtime())
        try {
            val daily = json.getAsJsonObject("daily")
            val times = daily.getAsJsonArray("time")
            val list = (0 until times.size()).map { i ->
                mapOf("date" to times[i].asString, "code" to daily.getAsJsonArray("weather_code")[i].asInt,
                    "max" to daily.getAsJsonArray("temperature_2m_max")[i].asDouble,
                    "min" to daily.getAsJsonArray("temperature_2m_min")[i].asDouble)
            }
            handler.post { if (!gate.isStopped && instance === this) dailyForecast = list }
        } catch (_: Exception) { /* 单独预报解析失败不清空缓存。 */ }
    }

    private fun ipGeolocation(): Pair<Double, Double>? = try {
        calls.fetch(Request.Builder().url("https://ipapi.co/json/").build()) { response ->
            if (!response.isSuccessful) null else {
                val json = gson.fromJson(response.body?.string(), JsonObject::class.java)
                val lat = json.get("latitude").asDouble
                val lng = json.get("longitude").asDouble
                if (lat in -90.0..90.0 && lng in -180.0..180.0) lat to lng else null
            }
        }
    } catch (_: Exception) { null }

    /** 按权限独立读取；步数使用聚合 API 去重，心率和睡眠读取全部分页。 */
    private suspend fun fetchHealth() {
        val day = LocalDate.now().toString()
        if (HealthConnectClient.getSdkStatus(this) != HealthConnectClient.SDK_AVAILABLE) {
            health = Health(day = day)
            return
        }
        val hc = healthConnectClient ?: HealthConnectClient.getOrCreate(this).also { healthConnectClient = it }
        val granted = withTimeoutOrNull(HEALTH_TIMEOUT_MS) { hc.permissionController.getGrantedPermissions() } ?: return
        val previous = health
        val now = Instant.now()
        val startOfDay = now.atZone(ZoneId.systemDefault()).toLocalDate().atStartOfDay(ZoneId.systemDefault()).toInstant()
        var heartRate = 0
        var steps: Long? = null
        var sleepHours = 0.0
        if (HealthPermission.getReadPermission(HeartRateRecord::class) in granted) {
            heartRate = try {
                withTimeoutOrNull(HEALTH_TIMEOUT_MS) {
                    val records = fetchRecordPages { token ->
                        hc.readRecords(ReadRecordsRequest(HeartRateRecord::class,
                            TimeRangeFilter.between(now.minus(6, ChronoUnit.HOURS), now), pageToken = token))
                            .let { it.records to it.pageToken }
                    }
                    records.asSequence().flatMap { it.samples.asSequence() }
                        .filter { it.time <= now && it.time >= now.minus(6, ChronoUnit.HOURS) }
                        .maxByOrNull { it.time }?.beatsPerMinute?.toInt() ?: 0
                } ?: previous.heart
            } catch (e: CancellationException) { throw e }
            catch (_: Exception) { previous.heart }
        }
        if (HealthPermission.getReadPermission(StepsRecord::class) in granted) {
            steps = try {
                withTimeoutOrNull(HEALTH_TIMEOUT_MS) {
                    hc.aggregate(AggregateRequest(setOf(StepsRecord.COUNT_TOTAL), TimeRangeFilter.between(startOfDay, now)))[StepsRecord.COUNT_TOTAL]
                }
            } catch (e: CancellationException) { throw e }
            catch (_: Exception) { previous.steps.takeIf { previous.day == day } }
        }
        if (HealthPermission.getReadPermission(SleepSessionRecord::class) in granted) {
            sleepHours = try {
                withTimeoutOrNull(HEALTH_TIMEOUT_MS) {
                    val records = fetchRecordPages { token ->
                        hc.readRecords(ReadRecordsRequest(SleepSessionRecord::class,
                            TimeRangeFilter.between(now.minus(24, ChronoUnit.HOURS), now), pageToken = token))
                            .let { it.records to it.pageToken }
                    }
                    val ranges = records.map { it.startTime.toEpochMilli() to it.endTime.toEpochMilli() }
                    totalSleepMillis(ranges, now.minus(24, ChronoUnit.HOURS).toEpochMilli(), now.toEpochMilli()) / 3600000.0
                } ?: previous.sleep
            } catch (e: CancellationException) { throw e }
            catch (_: Exception) { previous.sleep }
        }
        currentCoroutineContext().ensureActive()
        health = Health(heartRate, steps, sleepHours, day)
    }

    /** 一轮只查询一次前台应用，短缓存同时供位置精度和所有 usage 字段使用。 */
    private fun collectForeground() {
        val elapsed = SystemClock.elapsedRealtime()
        if (elapsed < nextForegroundCheck) return
        nextForegroundCheck = elapsed + FOREGROUND_INTERVAL_MS
        foregroundPackage = if (!hasUsageAccess()) "" else try {
            val usm = getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val now = System.currentTimeMillis()
            usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, now - 60000L, now)
                .maxByOrNull { it.lastTimeUsed }?.packageName ?: ""
        } catch (_: Exception) { "" }
        foregroundApp = if (foregroundPackage.isEmpty()) "未知" else try {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(foregroundPackage, 0)).toString()
        } catch (_: Exception) { foregroundPackage }
    }

    private fun isCalling(): Boolean {
        val cellular = try {
            @Suppress("DEPRECATION")
            val state = (getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager).callState
            state == TelephonyManager.CALL_STATE_OFFHOOK || state == TelephonyManager.CALL_STATE_RINGING
        } catch (_: Exception) { false }
        val voip = try {
            (getSystemService(Context.AUDIO_SERVICE) as android.media.AudioManager).mode == android.media.AudioManager.MODE_IN_COMMUNICATION
        } catch (_: Exception) { false }
        return cellular || voip
    }

    private fun hasUsageAccess(): Boolean = try {
        val appOps = getSystemService(Context.APP_OPS_SERVICE) as android.app.AppOpsManager
        @Suppress("DEPRECATION")
        val mode = appOps.checkOpNoThrow(android.app.AppOpsManager.OPSTR_GET_USAGE_STATS, android.os.Process.myUid(), packageName)
        mode == android.app.AppOpsManager.MODE_ALLOWED
    } catch (_: Exception) { false }

    private fun buildDiagnostics(): Map<String, Any> {
        val location = hasLocationPermission(this)
        val phoneState = hasPermission(this, Manifest.permission.READ_PHONE_STATE)
        val usageAccess = hasUsageAccess()
        val notification = Build.VERSION.SDK_INT < 33 || hasPermission(this, Manifest.permission.POST_NOTIFICATIONS)
        val warnings = mutableListOf<String>()
        if (!location) warnings.add("未授予定位权限，使用缓存或 IP 定位")
        if (!hasMotionPermission(this)) warnings.add("未授予运动识别权限，传感器步数不可用")
        if (!phoneState) warnings.add("未授予通话状态权限")
        if (!usageAccess) warnings.add("未开启使用情况访问权限")
        return mapOf("app_version" to BuildConfig.VERSION_NAME, "running_seconds" to ((SystemClock.elapsedRealtime() - startElapsed) / 1000),
            "send_success" to sendSuccess, "send_failed" to sendFailed, "last_error" to lastError,
            "permissions" to mapOf("location" to location, "phone_state" to phoneState, "usage_access" to usageAccess, "notification" to notification),
            "warnings" to warnings)
    }

    private fun stepsToday(): Long = stepState.let { if (it.day == LocalDate.now().toString() && hasMotionPermission(this)) it.total else 0L }

    override fun onSensorChanged(event: SensorEvent) {
        if (gate.isStopped || event.sensor.type != Sensor.TYPE_STEP_COUNTER) return
        val current = event.values.firstOrNull()?.toLong() ?: return
        if (current < 0) return
        val next = recordSteps(stepState, LocalDate.now().toString(), bootCount, current)
        stepState = next
        stepPrefs.edit().putString("day", next.day).putInt("boot", next.bootCount)
            .putLong("counter", next.counter).putLong("total", next.total).apply()
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    companion object {
        const val EXTRA_IP = "extra_ip"
        const val EXTRA_PORT = "extra_port"
        const val EXTRA_INTERVAL = "extra_interval"
        const val EXTRA_TOKEN = "extra_token"
        const val DEFAULT_IP = SensorConfig.DEFAULT_IP
        const val DEFAULT_PORT = SensorConfig.DEFAULT_PORT
        const val DEFAULT_INTERVAL = SensorConfig.DEFAULT_INTERVAL
        private const val NOTIFICATION_ID = 1
        private const val TAG = "MizukiSensor"
        private const val MAX_BACKOFF_MULTIPLIER = 8
        private const val LOCATION_INTERVAL_MS = 15000L
        private const val LOCATION_TIMEOUT_MS = 5000L
        private const val LOCATION_MAX_AGE_MS = 30 * 60 * 1000L
        private const val CITY_INTERVAL_MS = 5 * 60 * 1000L
        private const val HEALTH_INTERVAL_MS = 60000L
        private const val HEALTH_TIMEOUT_MS = 8000L
        private const val WEATHER_CACHE_MS = 15 * 60 * 1000L
        private const val WEATHER_RETRY_MS = 60000L
        private const val FOREGROUND_INTERVAL_MS = 1000L
        private const val REPLAY_INTERVAL_MS = 1000L
        private const val REPLAY_FAILURE_MS = 10000L
        private const val METADATA_TICK_MS = 1000L
        @Volatile var latestData: Map<String, Any>? = null
            private set
        @Volatile var dailyForecast: List<Map<String, Any>>? = null
            private set
        @Volatile var isRunning = false
            private set
        @Volatile var isConnected = false
            private set
        @Volatile var startupError = ""
            private set
        @Volatile private var instance: SensorService? = null

        fun requestRefresh() {
            instance?.let { svc -> svc.handler.post { if (!svc.gate.isStopped) svc.collectAndSend() } }
        }

        fun requestHealthRefresh() {
            instance?.let { svc ->
                if (!svc.gate.isStopped) svc.nextHealthCheck = 0L
            }
        }

        /** UI 先同步关闭旧运行期，阻止断开和重新连接之间的旧请求回流。 */
        fun stop(context: Context) {
            instance?.stopCollection()
            context.stopService(Intent(context, SensorService::class.java))
        }

        private fun hasPermission(context: Context, permission: String) =
            context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

        private fun hasLocationPermission(context: Context) = hasPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ||
            hasPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION)

        private fun hasMotionPermission(context: Context) = Build.VERSION.SDK_INT < 29 || hasPermission(context, Manifest.permission.ACTIVITY_RECOGNITION)

        fun foregroundTypes(context: Context): Int {
            var types = 0
            val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val enabled = try {
                if (Build.VERSION.SDK_INT >= 28) lm.isLocationEnabled else lm.isProviderEnabled(LocationManager.GPS_PROVIDER) || lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
            } catch (_: Exception) { false }
            val (location, health) = resolveForegroundAccess(Build.VERSION.SDK_INT, hasLocationPermission(context), enabled, hasMotionPermission(context))
            if (location) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
            if (health) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH
            return types
        }

        fun canStart(context: Context) = Build.VERSION.SDK_INT < 34 || foregroundTypes(context) != 0

        private fun isPermanentReject(code: Int) = code in 400..499 && code != 429

        private val WEATHER_MAP = mapOf(0..0 to "clear", 1..3 to "cloudy", 45..48 to "fog", 51..57 to "drizzle",
            61..67 to "rain", 71..77 to "snow", 80..82 to "shower", 85..86 to "snow", 95..99 to "thunderstorm")
        fun weatherCodeToCondition(code: Int): String = WEATHER_MAP.entries.firstOrNull { code in it.key }?.value ?: "unknown"
    }
}
