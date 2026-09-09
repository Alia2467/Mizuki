package com.mizuki.sensor

import okhttp3.Call
import okhttp3.Request
import okhttp3.Response
import java.io.IOException

/** 请求从创建到读完响应均受服务生命周期约束；停止后不允许新请求进入。 */
class SensorCalls(private val buildCall: (Request) -> Call) {
    private val lock = Any()
    private val calls = mutableSetOf<Call>()
    private var isStopped = false

    fun <T> fetch(request: Request, read: (Response) -> T): T {
        val call = buildCall(request)
        synchronized(lock) {
            if (isStopped) {
                call.cancel()
                throw IOException("采集服务已停止")
            }
            calls.add(call)
        }
        return try {
            call.execute().use(read)
        } finally {
            synchronized(lock) { calls.remove(call) }
        }
    }

    fun stop() {
        synchronized(lock) {
            isStopped = true
            calls.forEach { it.cancel() }
        }
    }
}
