package com.maibot.sensor

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity

/** 关于 maibot感知 页（内容先留白）。 */
class AboutActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_about)
        findViewById<View>(R.id.backButton).setOnClickListener { finish() }
    }
}
