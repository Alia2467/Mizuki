package com.mizuki.sensor

import kotlinx.coroutines.runBlocking
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Timeout
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** 离线 JVM 回归入口，不依赖未缓存的 JUnit；sensorRegression 会在 testDebugUnitTest 前执行。 */
fun main() {
    val saved = mapOf("ip" to "10.0.0.7", "port" to "9000", "interval" to "1000", "token" to "secret")
    val restored = SensorConfig.resolve(emptyMap(), saved)
    check(restored.host == "10.0.0.7" && restored.port == 9000 && restored.token == "secret")
    check(restored.intervalMs == 1000L)
    check(SensorConfig.resolve(mapOf("interval" to "1"), saved).intervalMs == 100L)
    check(SensorConfig.resolve(mapOf("interval" to "2147483647"), saved).intervalMs == 600000L)
    check(runCatching { SensorConfig.resolve(mapOf("port" to "70000"), saved) }.isFailure)
    val sequence = UploadSequence("test-session")
    val first = sequence.snapshot()
    val second = sequence.snapshot()
    check(first.sequence == 1L && second.sequence == 2L && first.session == second.session)
    val live = buildUploadRequest(restored, "{}", first)
    val replay = buildUploadRequest(restored, "{}", isReplay = true)
    check(live.header("X-Sensor-Session") == "test-session")
    check(live.header("X-Sensor-Sequence") == "1")
    check(live.header("X-Sensor-Replay") == null)
    check(replay.header("X-Sensor-Replay") == "true")
    check(replay.header("X-Sensor-Session") == null && replay.header("X-Sensor-Sequence") == null)
    check(replay.header("X-Sensor-Token") == "secret")
    println("PASS: configuration restoration, interval bounds and immutable upload identity")

    val gate = CollectionGate()
    check(gate.collect() && !gate.collect())
    gate.finish()
    check(gate.collect())
    gate.stop()
    gate.finish()
    check(!gate.collect())
    val initial = recordSteps(StepState(), "2026-09-09", 10, 100L)
    val walked = recordSteps(initial, "2026-09-09", 10, 140L)
    check(walked.total == 40L)
    val restoredSteps = recordSteps(walked.copy(), "2026-09-09", 10, 155L)
    check(restoredSteps.total == 55L)
    val rebooted = recordSteps(restoredSteps, "2026-09-09", 11, 160L)
    check(rebooted.total == 215L) // 重启后的计数大于旧值也不能漏掉新启动的步数。
    val tomorrow = recordSteps(rebooted, "2026-09-10", 11, 180L)
    check(tomorrow.total == 0L)
    check(recordSteps(tomorrow, "2026-09-10", 11, 190L).total == 10L)
    check(totalSleepMillis(listOf(0L to 20L, 10L to 30L, 0L to 20L), 5L, 25L) == 20L)
    check(totalSleepMillis(listOf(30L to 40L), 0L, 25L) == 0L)
    println("PASS: single-flight lifecycle, daily steps/reboot restoration and sleep clipping/deduplication")
    val network = BlockingCall(live)
    val calls = SensorCalls { network }
    val worker = Executors.newSingleThreadExecutor()
    try {
        val pending = worker.submit<Boolean> { runCatching { calls.fetch(live) { it.code } }.isFailure }
        check(network.entered.await(2, TimeUnit.SECONDS))
        calls.stop()
        check(pending.get(2, TimeUnit.SECONDS))
        check(network.isCanceled())
        check(runCatching { calls.fetch(live) { it.code } }.isFailure)
        check(network.executions == 1)
    } finally { calls.stop(); worker.shutdownNow() }
    val bodyCall = BlockingCall(live, hasDelay = false)
    val bodyCalls = SensorCalls { bodyCall }
    bodyCalls.fetch(live) {
        bodyCalls.stop()
        check(bodyCall.isCanceled()) // 响应返回后，读响应体期间仍必须可取消。
    }
    println("PASS: cancel active requests, reject late submissions, track response-body lifetime")
    check(resolveForegroundAccess(34, false, false, false) == (false to false))
    check(resolveForegroundAccess(34, true, true, false) == (true to false))
    check(resolveForegroundAccess(34, true, false, true) == (false to true))
    check(resolveForegroundAccess(33, true, true, true) == (true to false))
    runBlocking {
        val tokens = mutableListOf<String?>()
        val records = fetchRecordPages<Int> { token ->
            tokens.add(token)
            when (token) {
                null -> listOf(1, 2) to "page-two"
                else -> listOf(3) to null
            }
        }
        check(records == listOf(1, 2, 3) && tokens == listOf(null, "page-two"))
        check(runCatching { fetchRecordPages<Int> { listOf(1) to "repeated" } }.isFailure)
    }
    println("PASS: permission-dependent foreground types and complete/cycle-safe Health Connect pagination")
    val weatherCache = WeatherCache(900000L, 60000L)
    check(weatherCache.collect(0L))
    check(!weatherCache.collect(1000L)) // 无网络成功结果也必须冷却。
    weatherCache.record(Triple("rain", 22, 80), 1000L)
    check(!weatherCache.collect(900999L))
    check(weatherCache.collect(901000L))
    check(weatherCache.value == Triple("rain", 22, 80)) // 过期刷新失败保留旧值。
    check(!weatherCache.collect(902000L))
    println("PASS: weather success TTL, failed refresh cooldown and stale-cache fallback")
}

private class BlockingCall(private val value: Request, private val hasDelay: Boolean = true) : Call {
    val entered = CountDownLatch(1)
    private val cancelled = CountDownLatch(1)
    @Volatile var executions = 0
        private set
    override fun request() = value
    override fun execute(): Response {
        executions++
        entered.countDown()
        if (hasDelay) {
            check(cancelled.await(2, TimeUnit.SECONDS))
            throw IOException("cancelled")
        }
        return Response.Builder().request(value).protocol(Protocol.HTTP_1_1).code(200).message("OK")
            .body("{}".toResponseBody()).build()
    }
    override fun enqueue(responseCallback: Callback) = error("异步入队不属于串行上传接口")
    override fun cancel() { cancelled.countDown() }
    override fun isExecuted() = executions > 0
    override fun isCanceled() = cancelled.count == 0L
    override fun timeout() = Timeout.NONE
    override fun clone(): Call = BlockingCall(value, hasDelay)
}
