package com.redstars.app

import android.content.Context
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.gpu.GpuDelegate
import org.tensorflow.lite.nnapi.NnApiDelegate
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.MappedByteBuffer
import java.nio.channels.FileChannel

/**
 * Inférence du codec 32×32 via TFLite (delegate GPU → NNAPI → CPU), en FLOAT et
 * en INT8 (PTQ, bit-exact vs float vérifié). Appelé depuis le helper Python
 * (Chaquopy) sur Android. Les modèles int8 ont la MÊME signature I/O que les
 * float (input float32, output int32) → le code de décode est partagé.
 *
 * Layout binaire identique à codec_numpy (vérifié bit-exact en Python) :
 *   decode : 1024 o latent → 8192 bits MSB-first → NHWC [32,32,8] →
 *            dec.tflite → argmax → [32,32] indices → 1024 o.
 */
object CodecGpu {
    private const val B = 64
    private const val PATCH = 1024
    private const val BITS = 8192

    private var enc: Interpreter? = null
    private var dec: Interpreter? = null
    private var encI8: Interpreter? = null
    private var decI8: Interpreter? = null
    @Volatile private var backendName = "none"
    @Volatile private var backendI8 = "none"
    @Volatile private var loadError: String? = null

    @JvmStatic
    @Synchronized
    fun init(ctx: Context) {
        if (dec != null || loadError != null) return
        try {
            // modèles FLOAT (requis)
            val (fp, fmode) = loadPair(loadAsset(ctx, "enc32.tflite"), loadAsset(ctx, "dec32.tflite"))
            if (fp == null) { loadError = "float : aucun backend TFLite n'a pu charger"; return }
            enc = fp.first; dec = fp.second; backendName = fmode
            // modèles INT8 (best-effort, pour le bench / le gain NPU)
            try {
                val (ip, imode) = loadPair(loadAsset(ctx, "enc32_int8.tflite"), loadAsset(ctx, "dec32_int8.tflite"))
                if (ip != null) { encI8 = ip.first; decI8 = ip.second; backendI8 = imode }
            } catch (t: Throwable) { backendI8 = "absent" }
        } catch (e: Throwable) {
            loadError = "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    private fun loadPair(encBuf: MappedByteBuffer, decBuf: MappedByteBuffer): Pair<Pair<Interpreter, Interpreter>?, String> {
        for (mode in listOf("gpu", "nnapi", "cpu")) {
            try {
                val e = Interpreter(encBuf, optionsFor(mode))
                val d = Interpreter(decBuf, optionsFor(mode))
                return Pair(Pair(e, d), mode)
            } catch (t: Throwable) { /* try next */ }
        }
        return Pair(null, "none")
    }

    private fun optionsFor(mode: String): Interpreter.Options {
        val o = Interpreter.Options()
        when (mode) {
            "gpu" -> o.addDelegate(GpuDelegate())
            "nnapi" -> o.addDelegate(NnApiDelegate())
            else -> o.setNumThreads(4)
        }
        return o
    }

    @JvmStatic
    fun status(): String =
        loadError?.let { "error: $it" } ?: "ok float=$backendName int8=$backendI8"

    private fun loadAsset(ctx: Context, name: String): MappedByteBuffer {
        ctx.assets.openFd(name).use { afd ->
            FileInputStream(afd.fileDescriptor).use { fis ->
                return fis.channel.map(FileChannel.MapMode.READ_ONLY, afd.startOffset, afd.declaredLength)
            }
        }
    }

    private fun newFloatIn() = ByteBuffer.allocateDirect(B * 32 * 32 * 8 * 4).order(ByteOrder.nativeOrder())
    private fun newIntOut() = ByteBuffer.allocateDirect(B * 32 * 32 * 4).order(ByteOrder.nativeOrder())

    /** Décode avec l'interpréteur fourni (float ou int8, même I/O). latent (N*1024) → indices (N*1024). */
    private fun decodeWith(d: Interpreter, latentBytes: ByteArray): ByteArray {
        val n = latentBytes.size / PATCH
        val out = ByteArray(n * PATCH)
        val inBuf = newFloatIn()
        val outBuf = newIntOut()
        val zero = FloatArray(32 * 32 * 8)
        var batch = 0
        while (batch < n) {
            val bsz = minOf(B, n - batch)
            inBuf.rewind()
            val fin = inBuf.asFloatBuffer()
            for (p in 0 until B) {
                if (p < bsz) {
                    val base = (batch + p) * PATCH
                    val pf = FloatArray(32 * 32 * 8)
                    for (k in 0 until BITS) {
                        val bit = (latentBytes[base + (k ushr 3)].toInt() ushr (7 - (k and 7))) and 1
                        val rem = k and 1023; val h = rem ushr 5; val w = rem and 31; val c = k ushr 10
                        pf[(h * 32 + w) * 8 + c] = bit.toFloat()
                    }
                    fin.put(pf)
                } else fin.put(zero)
            }
            inBuf.rewind(); outBuf.rewind()
            d.run(inBuf, outBuf)
            outBuf.rewind()
            val iout = outBuf.asIntBuffer()
            for (p in 0 until bsz) {
                val base = (batch + p) * PATCH; val off = p * 1024
                for (hw in 0 until 1024) out[base + hw] = iout.get(off + hw).toByte()
            }
            batch += bsz
        }
        return out
    }

    /** latentBytes (N*1024) → indexBytes (N*1024). Utilise le décodeur FLOAT. */
    @JvmStatic
    @Synchronized
    fun decode(latentBytes: ByteArray): ByteArray {
        val d = dec ?: throw IllegalStateException("CodecGpu non init (${status()})")
        return decodeWith(d, latentBytes)
    }

    /** Décode via le modèle INT8 (NPU). Fallback float si int8 absent. */
    @JvmStatic
    @Synchronized
    fun decodeI8(latentBytes: ByteArray): ByteArray {
        val d = decI8 ?: dec ?: throw IllegalStateException("CodecGpu non init (${status()})")
        return decodeWith(d, latentBytes)
    }

    /** patchBytes (N*1024 indices) → latentBytes (N*1024). Encodeur FLOAT. */
    @JvmStatic
    @Synchronized
    fun encode(patchBytes: ByteArray): ByteArray {
        val e = enc ?: throw IllegalStateException("CodecGpu non init (${status()})")
        val n = patchBytes.size / PATCH
        val out = ByteArray(n * PATCH)
        val inBuf = newFloatIn()
        val outBuf = newFloatIn()
        val zero = FloatArray(32 * 32 * 8)
        var batch = 0
        while (batch < n) {
            val bsz = minOf(B, n - batch)
            inBuf.rewind()
            val fin = inBuf.asFloatBuffer()
            for (p in 0 until B) {
                if (p < bsz) {
                    val base = (batch + p) * PATCH
                    val pf = FloatArray(32 * 32 * 8)
                    for (hw in 0 until 1024) {
                        val v = patchBytes[base + hw].toInt() and 0xFF
                        val o = hw * 8
                        for (c in 0 until 8) pf[o + c] = ((v ushr c) and 1).toFloat()
                    }
                    fin.put(pf)
                } else fin.put(zero)
            }
            inBuf.rewind(); outBuf.rewind()
            e.run(inBuf, outBuf)
            outBuf.rewind()
            val fout = outBuf.asFloatBuffer()
            for (p in 0 until bsz) {
                val base = (batch + p) * PATCH
                for (byteI in 0 until 1024) {
                    var b = 0
                    for (bit in 0 until 8) {
                        val k = byteI * 8 + bit
                        val rem = k and 1023; val h = rem ushr 5; val w = rem and 31; val c = k ushr 10
                        if (fout.get(p * (32 * 32 * 8) + (h * 32 + w) * 8 + c) >= 0.5f) b = b or (1 shl (7 - bit))
                    }
                    out[base + byteI] = b.toByte()
                }
            }
            batch += bsz
        }
        return out
    }

    /** Bench FLOAT vs INT8 du décode sur n latents aléatoires : Mo/s, speedup, mismatch int8/float. */
    @JvmStatic
    fun benchInt8(n: Int): String {
        try {
            val d = dec ?: return "float dec non chargé (${status()})"
            val di8 = decI8 ?: return "int8 dec non chargé (asset enc32_int8/dec32_int8 manquant ?) — ${status()}"
            val rnd = java.util.Random(2)
            val lat = ByteArray(n * PATCH).also { rnd.nextBytes(it) }
            decodeWith(d, ByteArray(PATCH)); decodeWith(di8, ByteArray(PATCH))  // warmup
            var t = System.nanoTime()
            val of = decodeWith(d, lat)
            val fMs = (System.nanoTime() - t) / 1e6
            t = System.nanoTime()
            val oi = decodeWith(di8, lat)
            val iMs = (System.nanoTime() - t) / 1e6
            var mism = 0
            for (k in 0 until n * PATCH) if (of[k] != oi[k]) mism++
            val mb = n * 1024.0 / 1e6
            return "n=$n | FLOAT[$backendName] ${"%.0f".format(fMs)}ms=${"%.2f".format(mb / (fMs / 1000))}Mo/s " +
                "| INT8[$backendI8] ${"%.0f".format(iMs)}ms=${"%.2f".format(mb / (iMs / 1000))}Mo/s " +
                "| speedup=${"%.2f".format(fMs / iMs)}x | int8_vs_float_mismatch=$mism/${n * PATCH}o"
        } catch (e: Throwable) {
            return "benchInt8 error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /** Self-test : round-trip dec(enc(x)) sur le chemin FLOAT. */
    @JvmStatic
    fun selfTest(n: Int): String {
        try {
            val rnd = java.util.Random(1)
            val patches = ByteArray(n * PATCH).also { rnd.nextBytes(it) }
            decode(encode(ByteArray(PATCH).also { rnd.nextBytes(it) }))
            var t = System.nanoTime()
            val lat = encode(patches)
            val encMs = (System.nanoTime() - t) / 1e6
            t = System.nanoTime()
            val back = decode(lat)
            val decMs = (System.nanoTime() - t) / 1e6
            var mism = 0
            for (i in 0 until n * PATCH) if (back[i] != patches[i]) mism++
            val mb = n * 1024.0 / 1e6
            return "backend=$backendName n=$n enc=${"%.0f".format(encMs)}ms(${"%.1f".format(mb / (encMs / 1000))}Mo/s) " +
                "dec=${"%.0f".format(decMs)}ms(${"%.1f".format(mb / (decMs / 1000))}Mo/s) mism=$mism/${n * PATCH}o"
        } catch (e: Throwable) {
            return "selfTest error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }
}
