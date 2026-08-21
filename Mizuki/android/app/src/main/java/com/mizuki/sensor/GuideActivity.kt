package com.mizuki.sensor

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity

/** 用户指南页。 */
class GuideActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_guide)
        findViewById<View>(R.id.backButton).setOnClickListener { finish() }
    }
}
