package com.mizuki.sensor

import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.UUID

/** 请求发起时即进入失败冷却；失败不覆盖旧值，成功后才延长到正常缓存时间。 */
class WeatherCache(private val lifetimeMs: Long, private val retryMs: Long) {
    @Volatile var value = Triple("unknown", 0, 0)
        private set
    private var nextAttempt = 0L

    fun collect(now: Long): Boolean {
        if (now < nextAttempt) return false
        nextAttempt = now + retryMs
        return true
    }

    fun record(result: Triple<String, Int, Int>, now: Long) {
        if (result.first == "unknown") return
        value = result
        nextAttempt = now + lifetimeMs
    }
}

fun resolveForegroundAccess(api: Int, hasLocation: Boolean, isLocationEnabled: Boolean, hasMotion: Boolean): Pair<Boolean, Boolean> =
    (hasLocation && isLocationEnabled) to (api >= 34 && hasMotion)

/** Health Connect 单次 readRecords 不代表全部记录；空页仍继续读取非空的下一页令牌。 */
suspend fun <T> fetchRecordPages(fetch: suspend (String?) -> Pair<List<T>, String?>): List<T> {
    var token: String? = null
    val seen = mutableSetOf<String>()
    val records = mutableListOf<T>()
    do {
        val (page, next) = fetch(token)
        records.addAll(page)
        check(next == null || seen.add(next)) { "健康数据分页令牌重复" }
        token = next
    } while (token != null)
    return records
}

/** 主动刷新与周期采集共用门控；停止不可逆，迟到回调不能重新打开服务。 */
class CollectionGate {
    @Volatile var isStopped = false
        private set
    private var isCollecting = false

    @Synchronized fun collect(): Boolean {
        if (isStopped || isCollecting) return false
        isCollecting = true
        return true
    }
    @Synchronized fun finish() { isCollecting = false }
    @Synchronized fun stop() { isStopped = true }
}

data class StepState(val day: String = "", val bootCount: Int = -1, val counter: Long = -1, val total: Long = 0)

/** 跨日重新建立基线；同日设备重启保留已观测总量，补上本次开机后的计数。 */
fun recordSteps(previous: StepState, day: String, bootCount: Int, counter: Long): StepState {
    val total = when {
        previous.day != day || previous.counter < 0 -> 0L
        (bootCount >= 0 && previous.bootCount >= 0 && bootCount != previous.bootCount) || counter < previous.counter -> previous.total + counter
        else -> previous.total + counter - previous.counter
    }
    return StepState(day, bootCount, counter, total.coerceAtLeast(0))
}

/** 裁剪窗口并合并重叠睡眠区间，避免跨窗整段计入和多来源重复累计。 */
fun totalSleepMillis(ranges: List<Pair<Long, Long>>, start: Long, end: Long): Long {
    val clipped = ranges.map { maxOf(it.first, start) to minOf(it.second, end) }
        .filter { it.second > it.first }.sortedBy { it.first }
    var total = 0L
    var lastEnd = start
    for ((from, to) in clipped) {
        total += (to - maxOf(from, lastEnd)).coerceAtLeast(0)
        lastEnd = maxOf(lastEnd, to)
    }
    return total
}

/** 实际连接配置：显式参数 → 保存值 → 默认值；所有启动入口共用。 */
data class SensorConfig(val host: String, val port: Int, val intervalMs: Long, val token: String) {
    fun uploadUrl(): HttpUrl = HttpUrl.Builder().scheme("http").host(host).port(port)
        .addPathSegment("phone-data").build()

    companion object {
        const val DEFAULT_IP = "192.168.1.4"
        const val DEFAULT_PORT = 821
        const val DEFAULT_INTERVAL = 300
        const val MIN_INTERVAL_MS = 100L
        const val MAX_INTERVAL_MS = 600000L

        fun resolve(explicit: Map<String, String>, saved: Map<String, String>): SensorConfig {
            fun value(key: String, default: String) = (explicit[key] ?: saved[key] ?: default).trim()
            val host = value("ip", DEFAULT_IP)
            val port = value("port", DEFAULT_PORT.toString()).toIntOrNull()
            require(port != null && port in 1..65535) { "端口必须在 1–65535 之间" }
            val interval = (value("interval", DEFAULT_INTERVAL.toString()).toLongOrNull()
                ?: DEFAULT_INTERVAL.toLong()).coerceIn(MIN_INTERVAL_MS, MAX_INTERVAL_MS)
            val token = value("token", "")
            require(token.all { it in ' '..'~' }) { "令牌包含 HTTP 请求头不支持的字符" }
            return SensorConfig(host, port, interval, token).also { it.uploadUrl() }
        }
    }
}

/** 序号在构造样本时固定，不依赖墙上时钟，也不在网络回调里重新取值。 */
data class UploadStamp(val session: String, val sequence: Long)

class UploadSequence(private val session: String = UUID.randomUUID().toString()) {
    private var sequence = 0L

    @Synchronized
    fun snapshot(): UploadStamp = UploadStamp(session, ++sequence)
}

fun buildUploadRequest(
    config: SensorConfig,
    payload: String,
    stamp: UploadStamp? = null,
    isReplay: Boolean = false,
): Request {
    require((stamp != null) != isReplay) { "实时样本必须有序号，历史补传不能有实时序号" }
    return Request.Builder().url(config.uploadUrl())
        .post(payload.toRequestBody("application/json; charset=utf-8".toMediaType()))
        .apply {
            if (config.token.isNotEmpty()) header("X-Sensor-Token", config.token)
            if (isReplay) {
                header("X-Sensor-Replay", "true")
            } else {
                header("X-Sensor-Session", stamp!!.session)
                header("X-Sensor-Sequence", stamp.sequence.toString())
            }
        }.build()
}
