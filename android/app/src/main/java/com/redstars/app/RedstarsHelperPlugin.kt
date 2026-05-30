package com.redstars.app

import android.os.Environment
import android.util.Log
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.File
import java.util.concurrent.atomic.AtomicReference

/**
 * Capacitor plugin qui embarque le helper.py desktop dans l'APK via
 * Chaquopy.
 *
 * Au load :
 *   1. Démarre la VM Python (idempotent — Chaquopy détecte déjà-démarré).
 *   2. Spawn un thread daemon qui exécute `helper.main()`. helper.py
 *      tourne sa propre HTTPServer en boucle ; on doit le sortir du
 *      thread UI sinon l'app se fige au démarrage.
 *   3. Expose `getStatus()` au JS — utilisé par AgentStatus côté
 *      WebView (cap?.Plugins?.RedstarsHelper.getStatus()).
 *
 * Phase 2 (à venir) : remplacer le bundled helper.py par celui du cache
 * app-privé quand /script-py-v* a une version plus récente — même
 * mécanisme que script_updater.rs côté Tauri.
 *
 * Phase 3 (à venir) : enregistrer un service foreground pour que le
 * helper survive un swipe-away depuis la liste des apps récentes.
 */
@CapacitorPlugin(name = "RedstarsHelper")
class RedstarsHelperPlugin : Plugin() {

    companion object {
        private const val TAG = "RedstarsHelper"
        // Version du shell APK — affichée à la UI, pas le helper.py
        // (cette version-là est dans helper.py lui-même).
        private const val SHELL_VERSION = "0.2.0-android"
        // Erreur de démarrage si crash au boot. Atomic : lu depuis le
        // thread Capacitor (call.resolve), écrit depuis le thread Python.
        private val startupError = AtomicReference<String?>(null)
        @Volatile private var started = false
    }

    override fun load() {
        super.load()
        val ctx = context
        try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(ctx))
            }
            // Cache app-privé : tous les états runtime du helper (refs/,
            // iso/, decoded/, certs matérialisés) atterrissent ici.
            val cacheDir = File(ctx.cacheDir, "redstars-helper").apply { mkdirs() }
            // helper.py lit ces 2 env vars pour savoir où écrire ; on
            // les passe à la VM Python via `os.environ` au démarrage.
            val py = Python.getInstance()
            val osModule = py.getModule("os")
            val environ = osModule["environ"]!!
            environ.callAttr("__setitem__", "XDG_CACHE_HOME", cacheDir.parentFile?.absolutePath ?: cacheDir.absolutePath)
            environ.callAttr("__setitem__", "REDSTARS_HELPER_PLATFORM", "android")
            // Spawn helper.main() en daemon — il a sa boucle HTTPServer
            // propre et bind 0.0.0.0:49080 + 49443.
            Thread {
                try {
                    py.getModule("helper").callAttr("main")
                } catch (e: PyException) {
                    Log.e(TAG, "helper.py crashed", e)
                    startupError.set("helper.py: ${e.message}")
                } catch (e: Throwable) {
                    Log.e(TAG, "Python thread died", e)
                    startupError.set("python thread: ${e.message}")
                }
            }.apply {
                isDaemon = true
                name = "redstars-helper"
                start()
            }
            started = true
            Log.i(TAG, "Python helper started in background thread")
        } catch (e: Throwable) {
            Log.e(TAG, "Failed to start Python runtime", e)
            startupError.set("python init: ${e.message}")
        }
    }

    /**
     * Lu côté WebView via `window.Capacitor.Plugins.RedstarsHelper.getStatus()`.
     * AgentStatus / Files Landing peuvent court-circuiter le probe HTTP
     * (lourd en CORS) en passant directement par ce bridge.
     */
    @PluginMethod
    fun getStatus(call: PluginCall) {
        val result = JSObject()
        result.put("ok", started && startupError.get() == null)
        result.put("shellVersion", SHELL_VERSION)
        // scriptVersion : helper.py inscrit sa VERSION dans son module ;
        // on la relit dynamiquement pour qu'un auto-update soit visible
        // sans rebuilder l'APK.
        try {
            val pyVer = Python.getInstance().getModule("helper").get("VERSION")?.toString()
            result.put("scriptVersion", pyVer ?: "unknown")
        } catch (e: Throwable) {
            result.put("scriptVersion", "unloaded")
        }
        startupError.get()?.let { result.put("error", it) }
        call.resolve(result)
    }
}
