package com.redstars.app

import android.content.Context
import android.util.Base64
import android.util.Log
import org.bouncycastle.crypto.digests.Blake2bDigest
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Auto-updater for helper.py — Android port of script_updater.rs.
 *
 * Pipeline at app start (background thread) :
 *   1. List releases via GitHub API
 *   2. Filter to tags `script-py-v*`, keep the highest semver
 *   3. Fetch script-latest.json manifest from that release
 *   4. Compare version with our cached version (sidecar file)
 *   5. If newer, download helper.py + helper.py.minisig
 *   6. Verify sha256 + Ed25519 minisign signature with hardcoded pubkey
 *   7. Atomic write to cache dir (tmp → rename)
 *   8. Caller (RedstarsHelperPlugin) chooses to load from cache > bundled
 *
 * Any failure → keep previous cache. A bad signature is REJECTED, not
 * silently downgraded — that would mask tampering.
 */
object ScriptUpdater {
    private const val TAG = "ScriptUpdater"
    private const val GH_REPO = "delminator/redstars-helper"
    private const val FETCH_TIMEOUT_MS = 8_000
    private const val USER_AGENT = "redstars-mobile-helper-updater"

    /** Minisign Ed25519 pubkey — same identity as the Tauri shell.
     *  Format: "Ed" magic (2B) + key id (8B) + raw pubkey (32B), base64. */
    private const val PUBKEY_LINE =
        "RWSLOkiWKfscZzD9cOda4UFFRyOZJh5lu/lZZ56+oxa152FXiNtvuM/b"

    data class Manifest(
        val name: String,
        val version: String,
        val scriptUrl: String,
        val signatureUrl: String,
        val sha256: String,
        val size: Long,
    )

    /** Returns the path to load helper.py from RIGHT NOW.
     *  - cache file if it exists and the sidecar matches the manifest version
     *  - else null (caller falls back to bundled `from helper import main`). */
    fun cachedScriptPath(ctx: Context): File? {
        val f = scriptCacheFile(ctx)
        return if (f.isFile && f.length() > 0) f else null
    }

    /** Returns the recorded version of the cached script, or null if absent. */
    fun cachedScriptVersion(ctx: Context): String? {
        val v = versionMarkerFile(ctx)
        return if (v.isFile) v.readText().trim().ifBlank { null } else null
    }

    /** Main entry — call from a background thread. Returns true if a NEW
     *  version was successfully cached (caller decides whether to restart
     *  the Python helper subprocess). */
    fun checkAndUpdate(ctx: Context): Boolean {
        return try {
            val manifest = fetchLatestManifest() ?: run {
                Log.i(TAG, "no manifest available — skip")
                return false
            }
            val current = cachedScriptVersion(ctx)
            if (current == manifest.version) {
                Log.i(TAG, "already up-to-date at ${manifest.version}")
                return false
            }
            Log.i(TAG, "candidate $current → ${manifest.version}")
            val script = httpGetBytes(manifest.scriptUrl) ?: return false
            val sig = httpGetBytes(manifest.signatureUrl) ?: return false

            // sha256 sanity check.
            if (manifest.sha256.isNotEmpty()) {
                val got = sha256Hex(script)
                if (got != manifest.sha256) {
                    Log.e(TAG, "sha256 mismatch: expected ${manifest.sha256} got $got")
                    return false
                }
            }

            // Signature is the real gate. Bad sig → reject, keep previous.
            if (!verifyMinisign(script, sig)) {
                Log.e(TAG, "signature verify FAILED — keeping previous cache")
                return false
            }

            // Atomic write tmp → rename.
            val cacheDir = scriptCacheDir(ctx).apply { mkdirs() }
            val tmp = File(cacheDir, "helper.py.tmp")
            tmp.writeBytes(script)
            val dest = scriptCacheFile(ctx)
            if (!tmp.renameTo(dest)) {
                Log.e(TAG, "rename failed ${tmp.absolutePath} → ${dest.absolutePath}")
                return false
            }
            versionMarkerFile(ctx).writeText(manifest.version)
            Log.i(TAG, "cached v${manifest.version} at ${dest.absolutePath}")
            true
        } catch (e: Throwable) {
            Log.e(TAG, "update check failed", e)
            false
        }
    }

    // ─── private helpers ────────────────────────────────────────────────

    private fun scriptCacheDir(ctx: Context) = File(ctx.filesDir, "redstars-helper")
    private fun scriptCacheFile(ctx: Context) = File(scriptCacheDir(ctx), "helper.py")
    private fun versionMarkerFile(ctx: Context) = File(scriptCacheDir(ctx), "helper.py.version")

    /** List public releases, filter to script-py-v*, pick highest by semver,
     *  then fetch its script-latest.json. */
    private fun fetchLatestManifest(): Manifest? {
        val raw = httpGetString(
            "https://api.github.com/repos/$GH_REPO/releases?per_page=30"
        ) ?: return null
        val arr = JSONArray(raw)
        var bestTag: String? = null
        var bestVer = IntArray(3)
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            val tag = o.optString("tag_name", "")
            if (!tag.startsWith("script-py-v")) continue
            if (o.optBoolean("draft", false)) continue
            val ver = parseSemver(tag.removePrefix("script-py-v"))
            if (cmpSemver(ver, bestVer) > 0) {
                bestVer = ver
                bestTag = tag
            }
        }
        val tag = bestTag ?: return null
        val manifestUrl =
            "https://github.com/$GH_REPO/releases/download/$tag/script-latest.json"
        val manifestJson = httpGetString(manifestUrl) ?: return null
        val j = JSONObject(manifestJson)
        return Manifest(
            name        = j.optString("name", "helper.py"),
            version     = j.getString("version"),
            scriptUrl   = j.getString("script_url"),
            signatureUrl = j.getString("signature_url"),
            sha256      = j.optString("sha256", ""),
            size        = j.optLong("size", 0),
        )
    }

    private fun parseSemver(s: String): IntArray {
        val parts = s.split('.', '-')
        return IntArray(3) { i -> parts.getOrNull(i)?.toIntOrNull() ?: 0 }
    }
    private fun cmpSemver(a: IntArray, b: IntArray): Int {
        for (i in 0..2) {
            if (a[i] != b[i]) return a[i].compareTo(b[i])
        }
        return 0
    }

    private fun httpGetString(url: String): String? = try {
        openConnection(url).use { it.inputStream.bufferedReader().readText() }
    } catch (e: Throwable) {
        Log.w(TAG, "GET $url failed: ${e.message}")
        null
    }

    private fun httpGetBytes(url: String): ByteArray? = try {
        openConnection(url).use { it.inputStream.readBytes() }
    } catch (e: Throwable) {
        Log.w(TAG, "GET $url failed: ${e.message}")
        null
    }

    private fun openConnection(url: String): HttpURLConnection {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = FETCH_TIMEOUT_MS
        c.readTimeout = FETCH_TIMEOUT_MS * 2
        c.setRequestProperty("User-Agent", USER_AGENT)
        c.setRequestProperty("Accept", "application/vnd.github+json")
        c.instanceFollowRedirects = true
        if (c.responseCode !in 200..299) {
            val msg = "HTTP ${c.responseCode} on $url"
            c.disconnect()
            throw RuntimeException(msg)
        }
        return c
    }
    private inline fun <T> HttpURLConnection.use(block: (HttpURLConnection) -> T): T =
        try { block(this) } finally { disconnect() }

    private fun sha256Hex(bytes: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(bytes)
        val sb = StringBuilder(d.size * 2)
        for (b in d) sb.append("%02x".format(b))
        return sb.toString()
    }

    /** Verify a minisign signature. Handles BOTH algos minisign emits:
     *    - modern "ED" (Ed25519 over BLAKE2b-512(content)) — what
     *      `minisign -S` produces by default since v0.10, what our CI emits
     *    - legacy "Ed" (Ed25519 over raw content bytes)
     *
     *  Pubkey line format (base64): algo (2B) + keyId (8B) + pubkey (32B)
     *  Signature file format (text):
     *    untrusted comment: <…>
     *    base64(algo (2B) + keyId (8B) + 64B signature)
     *    trusted comment: <…>
     *    base64(64B trusted-comment signature)        ← we don't verify this
     *  We only verify the FIRST base64 line. */
    private fun verifyMinisign(script: ByteArray, signature: ByteArray): Boolean {
        return try {
            val pubBytes = Base64.decode(PUBKEY_LINE, Base64.DEFAULT)
            if (pubBytes.size != 42) {
                Log.e(TAG, "bad pubkey length ${pubBytes.size}, expected 42")
                return false
            }
            val pubKeyId = pubBytes.sliceArray(2..9)
            val pubKey = Ed25519PublicKeyParameters(pubBytes, 10)

            // Parse signature file (text), find first base64 line that's not
            // a comment.
            val sigText = String(signature, Charsets.UTF_8)
            val sigLine = sigText.lineSequence()
                .map { it.trim() }
                .filter { it.isNotEmpty() && !it.startsWith("untrusted") && !it.startsWith("trusted") }
                .firstOrNull() ?: run {
                Log.e(TAG, "no signature line found")
                return false
            }
            val sigBytes = Base64.decode(sigLine, Base64.DEFAULT)
            if (sigBytes.size != 74) {
                Log.e(TAG, "bad sig length ${sigBytes.size}, expected 74")
                return false
            }
            val sigAlgo = sigBytes.sliceArray(0..1)
            val sigKeyId = sigBytes.sliceArray(2..9)
            if (!pubKeyId.contentEquals(sigKeyId)) {
                Log.e(TAG, "key id mismatch")
                return false
            }
            val ed25519Sig = sigBytes.sliceArray(10..73)

            // ED → BLAKE2b-512 prehash ; Ed (legacy) → raw bytes.
            val msg = if (sigAlgo[0] == 'E'.code.toByte() && sigAlgo[1] == 'D'.code.toByte()) {
                val digest = Blake2bDigest(512)
                digest.update(script, 0, script.size)
                val out = ByteArray(64)
                digest.doFinal(out, 0)
                out
            } else {
                script
            }

            val verifier = Ed25519Signer()
            verifier.init(false, pubKey)
            verifier.update(msg, 0, msg.size)
            verifier.verifySignature(ed25519Sig)
        } catch (e: Throwable) {
            Log.e(TAG, "verifyMinisign threw", e)
            false
        }
    }
}
