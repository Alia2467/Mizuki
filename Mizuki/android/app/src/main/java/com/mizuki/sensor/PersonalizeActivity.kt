package com.mizuki.sensor

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.View
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/** 个性设置：自定义主题色（RGB 取色）+ 字体选择。 */
class PersonalizeActivity : AppCompatActivity() {

    private val prefs by lazy { getSharedPreferences("mizuki", Context.MODE_PRIVATE) }
    private lateinit var preview: View
    private lateinit var hexText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_personalize)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        preview = findViewById(R.id.colorPreview)
        hexText = findViewById(R.id.hexText)
        val redSeek = findViewById<SeekBar>(R.id.redSeek)
        val greenSeek = findViewById<SeekBar>(R.id.greenSeek)
        val blueSeek = findViewById<SeekBar>(R.id.blueSeek)

        val current = ThemeUtils.accentColor(this)
        redSeek.progress = Color.red(current)
        greenSeek.progress = Color.green(current)
        blueSeek.progress = Color.blue(current)

        val listener = object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, progress: Int, fromUser: Boolean) {
                update(redSeek, greenSeek, blueSeek)
            }

            override fun onStartTrackingTouch(sb: SeekBar?) {}
            override fun onStopTrackingTouch(sb: SeekBar?) {}
        }
        redSeek.setOnSeekBarChangeListener(listener)
        greenSeek.setOnSeekBarChangeListener(listener)
        blueSeek.setOnSeekBarChangeListener(listener)

        val fontSystem = findViewById<TextView>(R.id.fontSystem)
        val fontSans = findViewById<TextView>(R.id.fontSans)
        val fontSerif = findViewById<TextView>(R.id.fontSerif)
        fontSystem.setOnClickListener { chooseFont("default") }
        fontSans.setOnClickListener { chooseFont("sans") }
        fontSerif.setOnClickListener { chooseFont("serif") }

        update(redSeek, greenSeek, blueSeek)
        ThemeUtils.styleButtons(this, fontSystem, fontSans, fontSerif)
    }

    private fun update(r: SeekBar, g: SeekBar, b: SeekBar) {
        val color = Color.rgb(r.progress, g.progress, b.progress)
        val radius = 12f * resources.displayMetrics.density
        preview.background = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = radius
            setColor(color)
        }
        hexText.text = String.format(java.util.Locale.US, "#%02X%02X%02X", r.progress, g.progress, b.progress)
        ThemeUtils.saveAccent(this, color)
    }

    private fun chooseFont(code: String) {
        prefs.edit().putString("font", code).apply()
        recreate()
    }
}
