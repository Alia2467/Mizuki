package com.mizuki.sensor

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity

/** 关于 海月感知 页。 */
class AboutActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_about)
        findViewById<View>(R.id.backButton).setOnClickListener { finish() }
    }
}
