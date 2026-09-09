package com.mizuki.sensor

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log

/** 有界历史补传队列；只由后台线程访问，事务内插入/裁剪，关闭后永不重新打开。 */
class PendingStore(context: Context) : SQLiteOpenHelper(context, "pending.db", null, 1) {
    private var isClosed = false

    init { setWriteAheadLoggingEnabled(true) }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, created_at INTEGER NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    @Synchronized
    fun enqueue(payload: String) {
        if (isClosed) return
        try {
            val db = writableDatabase
            db.beginTransaction()
            try {
                db.execSQL("INSERT INTO pending (payload, created_at) VALUES (?, ?)", arrayOf(payload, System.currentTimeMillis()))
                db.execSQL("DELETE FROM pending WHERE id NOT IN (SELECT id FROM pending ORDER BY id DESC LIMIT ?)", arrayOf(MAX_PENDING))
                db.setTransactionSuccessful()
            } finally { db.endTransaction() }
        } catch (e: Exception) { Log.w(TAG, "历史载荷暂存失败", e) }
    }

    @Synchronized
    fun peek(limit: Int): List<Pair<Long, String>> {
        if (isClosed || limit <= 0) return emptyList()
        return try {
            val db = writableDatabase
            db.execSQL("DELETE FROM pending WHERE created_at < ?", arrayOf(System.currentTimeMillis() - TTL_MILLIS))
            val result = mutableListOf<Pair<Long, String>>()
            db.rawQuery("SELECT id, payload FROM pending ORDER BY id ASC LIMIT ?", arrayOf(limit.coerceAtMost(MAX_PENDING).toString())).use { c ->
                while (c.moveToNext()) result.add(c.getLong(0) to c.getString(1))
            }
            result
        } catch (e: Exception) {
            Log.w(TAG, "历史队列读取失败", e)
            emptyList()
        }
    }

    @Synchronized
    fun remove(id: Long) {
        if (isClosed) return
        try { writableDatabase.delete("pending", "id = ?", arrayOf(id.toString())) }
        catch (e: Exception) { Log.w(TAG, "历史载荷移除失败", e) }
    }

    @Synchronized
    override fun close() {
        if (isClosed) return
        isClosed = true
        super.close()
    }

    private companion object {
        const val MAX_PENDING = 500
        const val TTL_MILLIS = 24 * 60 * 60 * 1000L
        const val TAG = "MizukiPending"
    }
}
