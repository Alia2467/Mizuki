package com.mizuki.sensor

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.widget.TextView
import androidx.core.graphics.ColorUtils

/**
 * 主题色（自定义 RGB）：只作用于按键（按钮）的背景与文字。
 * 颜色存在 SharedPreferences 的 "accent_color" 键（ARGB int）。
 */
object ThemeUtils {

    val DEFAULT_ACCENT: Int = 0xFFFFFFFF.toInt() // 默认纯白色

    fun accentColor(context: Context): Int {
        return context.getSharedPreferences("mizuki", Context.MODE_PRIVATE)
            .getInt("accent_color", DEFAULT_ACCENT)
    }

    fun saveAccent(context: Context, color: Int) {
        context.getSharedPreferences("mizuki", Context.MODE_PRIVATE)
            .edit().putInt("accent_color", color).apply()
    }

    /** 生成按钮背景：圆角 + 主题色 + 略深描边。 */
    fun buttonDrawable(context: Context): GradientDrawable {
        val accent = accentColor(context)
        val density = context.resources.displayMetrics.density
        val radius = 12f * density
        val stroke = (1f * density).toInt().coerceAtLeast(1)
        val border = ColorUtils.blendARGB(accent, Color.BLACK, 0.20f)
        return GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = radius
            setColor(accent)
            setStroke(stroke, border)
        }
    }

    /** 根据底色亮度选择黑白文字，保证可读。 */
    fun contrastTextColor(accent: Int): Int =
        if (ColorUtils.calculateLuminance(accent) > 0.5f) Color.BLACK else Color.WHITE

    /** 应用主题色到一组按钮：背景 + 根据亮度自动选择黑白文字。 */
    fun styleButtons(context: Context, vararg buttons: View) {
        val accent = accentColor(context)
        val textColor = contrastTextColor(accent)
        for (b in buttons) {
            b.background = buttonDrawable(context)
            if (b is TextView) b.setTextColor(textColor)
        }
    }
}
