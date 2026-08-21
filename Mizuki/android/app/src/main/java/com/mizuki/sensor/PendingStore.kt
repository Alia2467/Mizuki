package com.mizuki.sensor

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * 离线补传队列：上报失败的 JSON 载荷暂存本地，恢复连接后按先进先出补传。
 *
 * 与 NoteDbHelper 一样使用系统 SQLite，不引入额外依赖。
 * 队列有界（最多 500 条，超出丢弃最旧）且只补传 24 小时内的记录，
 * 避免长期断网时无限膨胀与补传失去时效性的旧数据。
 */
class PendingStore(context: Context) : SQLiteOpenHelper(context, "pending.db", null, 1) {

    private companion object {
        const val MAX_PENDING = 500
        const val TTL_MILLIS = 24 * 60 * 60 * 1000L
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE IF NOT EXISTS pending (" +
                "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
                "payload TEXT NOT NULL, " +
                "created_at INTEGER NOT NULL)"
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // 无历史版本迁移
    }

    /** 暂存一条发送失败的载荷；超出容量上限时先丢弃最旧。 */
    fun enqueue(payload: String) {
        try {
            val db = writableDatabase
            db.execSQL(
                "DELETE FROM pending WHERE id NOT IN (SELECT id FROM pending ORDER BY id DESC LIMIT ?)",
                arrayOf((MAX_PENDING - 1).toString())
            )
            db.execSQL(
                "INSERT INTO pending (payload, created_at) VALUES (?, ?)",
                arrayOf(payload, System.currentTimeMillis())
            )
        } catch (e: Exception) {
            // 暂存失败仅放弃本条，不影响主流程
        }
    }

    /** 取最早的最多 limit 条待发记录（id → payload）；顺带清理超过保留期的记录。 */
    fun peek(limit: Int): List<Pair<Long, String>> {
        val result = mutableListOf<Pair<Long, String>>()
        try {
            val db = writableDatabase
            db.execSQL(
                "DELETE FROM pending WHERE created_at < ?",
                arrayOf((System.currentTimeMillis() - TTL_MILLIS).toString())
            )
            db.rawQuery(
                "SELECT id, payload FROM pending ORDER BY id ASC LIMIT ?",
                arrayOf(limit.toString())
            ).use { c ->
                while (c.moveToNext()) {
                    result.add(c.getLong(0) to c.getString(1))
                }
            }
        } catch (e: Exception) {
            // 读取失败返回空列表，下轮再试
        }
        return result
    }

    /** 补传成功后移除记录。 */
    fun remove(id: Long) {
        try {
            writableDatabase.delete("pending", "id = ?", arrayOf(id.toString()))
        } catch (e: Exception) {
            // 移除失败会导致重复补传，控制台侧按最新一条展示，可容忍
        }
    }
}
