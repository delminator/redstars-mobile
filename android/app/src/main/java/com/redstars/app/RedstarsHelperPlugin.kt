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
        private const val SHELL_VERSION = "0.2.6-android"
        // Erreur de démarrage si crash au boot. Atomic : lu depuis le
        // thread Capacitor (call.resolve), écrit depuis le thread Python.
        private val startupError = AtomicReference<String?>(null)
        @Volatile private var started = false
        // Vrai une fois que helper.main() a été spawné dans CE process.
        // Survit aux multiples appels à load() (Capacitor recrée
        // l'instance du plugin quand l'app revient au foreground après un
        // long sommeil) → on évite de relancer helper.main() qui
        // crasherait sur EADDRINUSE et marquerait à tort startupError.
        @Volatile private var helperSpawned = false
    }

    override fun load() {
        super.load()
        if (helperSpawned) {
            // Helper déjà lancé plus tôt dans ce process — Python tourne,
            // serve_forever() boucle, on a juste à refléter \`started\`.
            started = true
            Log.i(TAG, "load() re-entry : helper.main() déjà spawné, skip")
            return
        }
        helperSpawned = true
        val ctx = context
        // TOUT init Python tourne en background thread daemon. Sur le
        // premier lancement Chaquopy extrait ses libs natives
        // (libpython3.11.so ~5 Mo) — ça peut prendre 3-5 s, et la
        // synchronisation avec l'init du Capacitor Bridge fait que
        // l'Activity gèle → la WebView ne s'affiche pas du tout
        // (« page couldn't load »).
        Thread {
            try {
                // Inférence GPU du codec (TFLite) — init avant que le helper Python
                // ne l'appelle. Pur Kotlin, sans Python. Best-effort.
                try { CodecGpu.init(ctx) } catch (e: Throwable) { Log.w(TAG, "CodecGpu.init failed", e) }
                try { ImageGpu.init(ctx) } catch (e: Throwable) { Log.w(TAG, "ImageGpu.init failed", e) }
                if (!Python.isStarted()) {
                    Log.i(TAG, "Starting Python runtime (first launch may take 3-5 s)")
                    Python.start(AndroidPlatform(ctx))
                }
                // Cache app-privé : refs/, iso/, decoded/, certs matérialisés.
                val cacheDir = File(ctx.cacheDir, "redstars-helper").apply { mkdirs() }
                val py = Python.getInstance()
                val osModule = py.getModule("os")
                val environ = osModule["environ"]!!
                environ.callAttr(
                    "__setitem__", "XDG_CACHE_HOME",
                    cacheDir.parentFile?.absolutePath ?: cacheDir.absolutePath
                )
                environ.callAttr("__setitem__", "REDSTARS_HELPER_PLATFORM", "android")
                // Dossier de sortie ACCESSIBLE (USB / gestionnaire de fichiers, sans
                // permission) : getExternalFilesDir le crée, donc le helper Python peut
                // y écrire (sinon son test mkdir échouait → fallback cache non navigable).
                try {
                    val extSave = File(ctx.getExternalFilesDir(null), "RedStars").apply { mkdirs() }
                    environ.callAttr("__setitem__", "REDSTARS_HELPER_SAVE_DIR", extSave.absolutePath)
                } catch (e: Throwable) { Log.w(TAG, "getExternalFilesDir KO", e) }

                // Auto-update : on tente une mise à jour avant de lancer
                // helper.main(). Si le cache contient déjà la dernière
                // version, ça n'écrit rien ; sinon download + verify +
                // cache. Le check tourne en synchrone DANS ce thread (pas
                // le main thread) — ajoute au plus 1-2 s au boot froid.
                try {
                    ScriptUpdater.checkAndUpdate(ctx)
                } catch (e: Throwable) {
                    Log.w(TAG, "ScriptUpdater threw (continuing with bundled)", e)
                }
                started = true
                Log.i(TAG, "Python runtime ready, launching helper.main()")

                // Sélectionne le helper.py à exécuter :
                //   - cache (filesDir/redstars-helper/helper.py) si verify OK
                //   - bundle (assets/chaquopy/...) sinon
                // Pour charger depuis un path explicite on passe par
                // importlib.util — `py.getModule("helper")` ne lit que les
                // sources bundled dans l'APK.
                val cached = ScriptUpdater.cachedScriptPath(ctx)
                if (cached != null) {
                    val cachedVer = ScriptUpdater.cachedScriptVersion(ctx)
                    Log.i(TAG, "loading cached helper.py v$cachedVer from ${cached.absolutePath}")
                    val util = py.getModule("importlib.util")
                    val spec = util.callAttr("spec_from_file_location", "helper", cached.absolutePath)
                    val mod = util.callAttr("module_from_spec", spec)
                    spec["loader"]!!.callAttr("exec_module", mod)
                    mod.callAttr("main")
                } else {
                    Log.i(TAG, "loading bundled helper.py (no valid cache)")
                    py.getModule("helper").callAttr("main")
                }
            } catch (e: PyException) {
                Log.e(TAG, "helper.py crashed", e)
                startupError.set("helper.py: ${e.message}")
            } catch (e: Throwable) {
                Log.e(TAG, "Python init / helper thread died", e)
                startupError.set("python init: ${e.message}")
            }
        }.apply {
            isDaemon = true
            name = "redstars-helper"
            start()
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
        // scriptVersion : si on a un cache, on lit la version-marker
        // qui correspond au script effectivement chargé. Sinon on tombe
        // sur le bundled `helper.VERSION` (la valeur compilée dans l'APK).
        try {
            val cachedVer = ScriptUpdater.cachedScriptVersion(context)
            if (cachedVer != null) {
                result.put("scriptVersion", cachedVer)
                result.put("scriptSource", "cache")
            } else {
                val pyVer = Python.getInstance().getModule("helper").get("VERSION")?.toString()
                result.put("scriptVersion", pyVer ?: "unknown")
                result.put("scriptSource", "bundle")
            }
        } catch (e: Throwable) {
            result.put("scriptVersion", "unloaded")
        }
        startupError.get()?.let { result.put("error", it) }
        call.resolve(result)
    }
}
