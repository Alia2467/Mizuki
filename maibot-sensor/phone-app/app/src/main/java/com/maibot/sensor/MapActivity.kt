package com.maibot.sensor

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.View
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

/** 内置地图：本地 Leaflet + 高德瓦片，显示当前位置。 */
class MapActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var lat = 0.0
    private var lng = 0.0

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_map)

        findViewById<View>(R.id.backButton).setOnClickListener { finish() }

        webView = findViewById(R.id.mapWebView)
        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()

        readLocation()
        loadMap()

        findViewById<View>(R.id.mapLocateButton).setOnClickListener {
            readLocation()
            loadMap()
        }
        findViewById<View>(R.id.mapRefreshButton).setOnClickListener {
            SensorService.requestRefresh()
            webView.postDelayed({
                readLocation()
                loadMap()
            }, 2000)
        }
    }

    private fun readLocation() {
        val data = SensorService.latestData
        val loc = data?.get("location") as? Map<*, *>
        lat = (loc?.get("latitude") as? Number)?.toDouble() ?: 0.0
        lng = (loc?.get("longitude") as? Number)?.toDouble() ?: 0.0
    }

    private fun loadMap() {
        val centerLat = if (lat != 0.0) lat else 34.75
        val centerLng = if (lng != 0.0) lng else 113.65
        val html = buildHtml(centerLat, centerLng)
        webView.loadDataWithBaseURL("file:///android_asset/", html, "text/html", "utf-8", null)
    }

    private fun buildHtml(lat: Double, lng: Double): String {
        return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="stylesheet" href="leaflet.css" />
<script src="leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;padding:0}</style>
</head>
<body>
<div id="map"></div>
<script>
var lat = $lat, lng = $lng;
var map = L.map('map', {zoomControl: true}).setView([lat, lng], 15);
L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
  subdomains: ['1','2','3','4'], maxZoom: 18, attribution: '高德地图'
}).addTo(map);
L.circleMarker([lat, lng], {radius: 10, color: '#e94560', fillColor: '#e94560', fillOpacity: 0.8, weight: 2})
  .addTo(map).bindPopup('我的位置').openPopup();
</script>
</body>
</html>"""
    }
}
