package com.maibot.sensor

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.View
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import java.net.URLEncoder

/** 内置浏览器：用搜索词打开百度搜索结果。 */
class SearchActivity : AppCompatActivity() {

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_search)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        val query = intent.getStringExtra("query") ?: ""
        val webView = findViewById<WebView>(R.id.searchWebView)
        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()
        val url = if (query.isNotEmpty()) {
            "https://www.baidu.com/s?wd=" + URLEncoder.encode(query, "UTF-8")
        } else {
            "https://www.baidu.com"
        }
        webView.loadUrl(url)
    }
}
