package com.mizuki.sensor

import android.content.Context
import android.os.Bundle
import android.view.View
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/** 记事本：支持新建笔记 + 自定义分类。 */
class NoteActivity : AppCompatActivity() {

    private lateinit var db: NoteDbHelper
    private lateinit var categoryContainer: LinearLayout
    private lateinit var noteList: ListView
    private val categories = mutableListOf("全部")
    private var currentCategory = "全部"
    private val catPrefs by lazy { getSharedPreferences("note_cats", Context.MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_note)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        db = NoteDbHelper(this)
        categoryContainer = findViewById(R.id.categoryContainer)
        noteList = findViewById(R.id.noteList)

        val saved = catPrefs.getStringSet("cats", emptySet()) ?: emptySet()
        categories.addAll(saved.filter { it != "全部" && it.isNotEmpty() })

        findViewById<View>(R.id.newNoteButton).setOnClickListener { showNoteDialog(null) }
        findViewById<View>(R.id.newCategoryButton).setOnClickListener { showCategoryDialog() }

        refreshCategories()
        refreshNotes()
    }

    private fun refreshCategories() {
        categoryContainer.removeAllViews()
        val primary = ContextCompat.getColor(this, R.color.text_primary)
        val secondary = ContextCompat.getColor(this, R.color.text_secondary)
        for (cat in categories) {
            val chip = TextView(this).apply {
                text = cat
                textSize = 13f
                setTextColor(if (cat == currentCategory) primary else secondary)
                setBackgroundResource(if (cat == currentCategory) R.drawable.bg_box else R.drawable.bg_button)
                val pad = (12 * resources.displayMetrics.density).toInt()
                setPadding(pad, (6 * resources.displayMetrics.density).toInt(), pad, (6 * resources.displayMetrics.density).toInt())
                setOnClickListener {
                    currentCategory = cat
                    refreshCategories()
                    refreshNotes()
                }
            }
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
            lp.marginEnd = (8 * resources.displayMetrics.density).toInt()
            categoryContainer.addView(chip, lp)
        }
    }

    private fun refreshNotes() {
        val list = mutableListOf<String>()
        val readable = db.readableDatabase
        val cursor = if (currentCategory == "全部") {
            readable.rawQuery("SELECT _id, title, content FROM notes ORDER BY created_at DESC", null)
        } else {
            readable.rawQuery(
                "SELECT _id, title, content FROM notes WHERE category=? ORDER BY created_at DESC",
                arrayOf(currentCategory)
            )
        }
        while (cursor.moveToNext()) {
            val title = cursor.getString(1)
            val content = cursor.getString(2)
            list.add(if (title.isNotEmpty()) title else content.ifEmpty { "（空笔记）" })
        }
        cursor.close()
        noteList.adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, list)
        noteList.setOnItemClickListener { _, _, position, _ ->
            showNoteDetail(position)
        }
    }

    private fun showNoteDialog(editPosition: Int?) {
        val titleInput = EditText(this).apply { hint = getString(R.string.note_title) }
        val contentInput = EditText(this).apply {
            hint = getString(R.string.note_content)
            minLines = 4
        }
        if (editPosition != null) {
            val note = currentNotes()[editPosition]
            titleInput.setText(note.first)
            contentInput.setText(note.second)
        }
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 16, 32, 0)
            addView(titleInput)
            addView(contentInput)
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.new_note))
            .setView(container)
            .setPositiveButton("保存") { _, _ ->
                val title = titleInput.text.toString().trim()
                val content = contentInput.text.toString().trim()
                if (content.isEmpty() && title.isEmpty()) return@setPositiveButton
                val cat = if (currentCategory == "全部") "" else currentCategory
                if (editPosition == null) {
                    db.writableDatabase.execSQL(
                        "INSERT INTO notes (title, content, category, created_at) VALUES (?,?,?,?)",
                        arrayOf(title, content, cat, System.currentTimeMillis())
                    )
                } else {
                    val note = currentNotes()[editPosition]
                    db.writableDatabase.execSQL(
                        "UPDATE notes SET title=?, content=? WHERE _id=?",
                        arrayOf(title, content, note.third)
                    )
                }
                refreshNotes()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun showNoteDetail(position: Int) {
        val notes = currentNotes()
        if (position >= notes.size) return
        val (title, content, id) = notes[position]
        val display = if (title.isNotEmpty()) "$title\n\n$content" else content
        AlertDialog.Builder(this)
            .setTitle(title.ifEmpty { getString(R.string.note) })
            .setMessage(display.ifEmpty { "（空笔记）" })
            .setPositiveButton("编辑") { _, _ -> showNoteDialog(position) }
            .setNegativeButton("删除") { _, _ ->
                db.writableDatabase.execSQL("DELETE FROM notes WHERE _id=?", arrayOf(id))
                refreshNotes()
            }
            .setNeutralButton("关闭", null)
            .show()
    }

    private fun currentNotes(): List<Triple<String, String, String>> {
        val result = mutableListOf<Triple<String, String, String>>()
        val readable = db.readableDatabase
        val cursor = if (currentCategory == "全部") {
            readable.rawQuery("SELECT _id, title, content FROM notes ORDER BY created_at DESC", null)
        } else {
            readable.rawQuery(
                "SELECT _id, title, content FROM notes WHERE category=? ORDER BY created_at DESC",
                arrayOf(currentCategory)
            )
        }
        while (cursor.moveToNext()) {
            result.add(Triple(cursor.getString(1), cursor.getString(2), cursor.getString(0)))
        }
        cursor.close()
        return result
    }

    private fun showCategoryDialog() {
        val input = EditText(this).apply { hint = getString(R.string.category_name) }
        val container = LinearLayout(this).apply {
            setPadding(32, 16, 32, 0)
            addView(input)
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.new_category))
            .setView(container)
            .setPositiveButton("创建") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isEmpty() || categories.contains(name)) return@setPositiveButton
                categories.add(name)
                val set = categories.filter { it != "全部" }.toSet()
                catPrefs.edit().putStringSet("cats", set).apply()
                currentCategory = name
                refreshCategories()
                refreshNotes()
            }
            .setNegativeButton("取消", null)
            .show()
    }
}
