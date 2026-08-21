package com.maibot.sensor

import android.app.Activity
import android.content.Context
import android.graphics.Typeface
import android.view.View
import android.view.ViewGroup
import android.widget.TextView

/**
 * 字体管理：从 assets/fonts 加载开源中文字体，并递归应用到整个视图树。
 * 选择结果存在 SharedPreferences 的 "font" 键：default / sans / serif。
 */
object FontManager {

    @Volatile
    private var cachedSans: Typeface? = null

    @Volatile
    private var cachedSerif: Typeface? = null

    fun selected(context: Context): String =
        context.getSharedPreferences("maibot", Context.MODE_PRIVATE)
            .getString("font", "default") ?: "default"

    fun typeface(context: Context): Typeface {
        return when (selected(context)) {
            "sans" -> cachedSans ?: load(context, "fonts/noto_sans_sc.otf").also { cachedSans = it }
            "serif" -> cachedSerif ?: load(context, "fonts/noto_serif_sc.otf").also { cachedSerif = it }
            else -> Typeface.DEFAULT
        }
    }

    private fun load(context: Context, assetPath: String): Typeface {
        return try {
            Typeface.createFromAsset(context.assets, assetPath)
        } catch (e: Exception) {
            Typeface.DEFAULT
        }
    }

    /** 应用到单个 Activity 的内容根视图。 */
    fun applyTo(activity: Activity) {
        val root = activity.findViewById<View>(android.R.id.content) ?: return
        applyRecursive(root, typeface(activity))
    }

    private fun applyRecursive(view: View, tf: Typeface) {
        if (view is TextView) view.typeface = tf
        if (view is ViewGroup) {
            for (i in 0 until view.childCount) {
                applyRecursive(view.getChildAt(i), tf)
            }
        }
    }
}
