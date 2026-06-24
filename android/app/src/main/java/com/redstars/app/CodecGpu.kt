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
 * Inférence GPU du codec 32×32 via TFLite (delegate GPU → NNAPI → CPU).
 *
 * Appelé depuis le helper Python (Chaquopy) UNIQUEMENT sur Android :
 *   from com.redstars.app import CodecGpu
 *   CodecGpu.decode(latent_bytes) -> index_bytes
 *
 * Layout binaire identique à codec_numpy (vérifié bit-exact en Python) :
 *   - decode : par patch, 1024 o latent → 8192 bits MSB-first (np.unpackbits),
 *     rangés en NHWC [32,32,8] via k→(c=k/1024, h=(k%1024)/32, w=(k%1024)%32) ;
 *     dec32.tflite → argmax → [32,32] indices → 1024 o.
 *   - encode : 1024 o (indices) → bits LSB-first (to_bits : bit c = idx>>c) en
 *     NHWC ; enc32.tflite → round → 8192 bits packés MSB-first ordre NCHW → 1024 o.
 */
object CodecGpu {
    private const val B = 64            // batch fixe (modèles exportés en [64,32,32,8])
    private const val PATCH = 1024      // octets par patch / bloc latent
    private const val BITS = 8192

    private var enc: Interpreter? = null
    private var dec: Interpreter? = null
    @Volatile private var backendName = "none"
    @Volatile private var loadError: String? = null

    @JvmStatic
    @Synchronized
    fun init(ctx: Context) {
        if (dec != null || loadError != null) return
        try {
            val encBuf = loadAsset(ctx, "enc32.tflite")
            val decBuf = loadAsset(ctx, "dec32.tflite")
            // GPU → NNAPI → CPU (XNNPACK), chacun avec sa propre Options/delegate.
            for (mode in listOf("gpu", "nnapi", "cpu")) {
                try {
                    enc = Interpreter(encBuf, optionsFor(mode))
                    dec = Interpreter(decBuf, optionsFor(mode))
                    backendName = mode
                    return
                } catch (e: Throwable) {
                    enc = null; dec = null
                }
            }
            loadError = "aucun backend TFLite n'a pu charger"
        } catch (e: Throwable) {
            loadError = "${e.javaClass.simpleName}: ${e.message}"
        }
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
        loadError?.let { "error: $it" } ?: "ok backend=$backendName"

    private fun loadAsset(ctx: Context, name: String): MappedByteBuffer {
        ctx.assets.openFd(name).use { afd ->
            FileInputStream(afd.fileDescriptor).use { fis ->
                return fis.channel.map(FileChannel.MapMode.READ_ONLY, afd.startOffset, afd.declaredLength)
            }
        }
    }

    private fun newFloatIn() = ByteBuffer.allocateDirect(B * 32 * 32 * 8 * 4).order(ByteOrder.nativeOrder())
    private fun newIntOut() = ByteBuffer.allocateDirect(B * 32 * 32 * 4).order(ByteOrder.nativeOrder())

    /** latentBytes (N*1024) → indexBytes (N*1024). */
    @JvmStatic
    @Synchronized
    fun decode(latentBytes: ByteArray): ByteArray {
        val d = dec ?: throw IllegalStateException("CodecGpu non init (${status()})")
        val n = latentBytes.size / PATCH
        val out = ByteArray(n * PATCH)
        val inBuf = newFloatIn()
        val outBuf = newIntOut()
        var batch = 0
        while (batch < n) {
            val bsz = minOf(B, n - batch)
            inBuf.rewind()
            val fin = inBuf.asFloatBuffer()
            // remplit [B,32,32,8] (les patchs >= bsz restent à 0)
            val zero = FloatArray(32 * 32 * 8)
            for (p in 0 until B) {
                if (p < bsz) {
                    val base = (batch + p) * PATCH
                    val patchFloats = FloatArray(32 * 32 * 8)
                    for (k in 0 until BITS) {
                        val bit = (latentBytes[base + (k ushr 3)].toInt() ushr (7 - (k and 7))) and 1
                        val rem = k and 1023; val h = rem ushr 5; val w = rem and 31; val c = k ushr 10
                        patchFloats[(h * 32 + w) * 8 + c] = bit.toFloat()
                    }
                    fin.put(patchFloats)
                } else {
                    fin.put(zero)
                }
            }
            inBuf.rewind(); outBuf.rewind()
            d.run(inBuf, outBuf)
            outBuf.rewind()
            val iout = outBuf.asIntBuffer()
            for (p in 0 until bsz) {
                val base = (batch + p) * PATCH
                val off = p * 1024
                for (hw in 0 until 1024) out[base + hw] = iout.get(off + hw).toByte()
            }
            batch += bsz
        }
        return out
    }

    /** patchBytes (N*1024 indices) → latentBytes (N*1024). */
    @JvmStatic
    @Synchronized
    fun encode(patchBytes: ByteArray): ByteArray {
        val e = enc ?: throw IllegalStateException("CodecGpu non init (${status()})")
        val n = patchBytes.size / PATCH
        val out = ByteArray(n * PATCH)
        val inBuf = newFloatIn()
        val outBuf = newFloatIn()  // sortie enc = [B,32,32,8] float (bits 0/1)
        var batch = 0
        while (batch < n) {
            val bsz = minOf(B, n - batch)
            inBuf.rewind()
            val fin = inBuf.asFloatBuffer()
            val zero = FloatArray(32 * 32 * 8)
            for (p in 0 until B) {
                if (p < bsz) {
                    val base = (batch + p) * PATCH
                    val pf = FloatArray(32 * 32 * 8)
                    for (hw in 0 until 1024) {
                        val v = patchBytes[base + hw].toInt() and 0xFF
                        val o = hw * 8
                        for (c in 0 until 8) pf[o + c] = ((v ushr c) and 1).toFloat()  // to_bits LSB
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
                // packbits MSB-first en ordre NCHW : k→(c,h,w)
                for (byteI in 0 until 1024) {
                    var b = 0
                    for (bit in 0 until 8) {
                        val k = byteI * 8 + bit
                        val rem = k and 1023; val h = rem ushr 5; val w = rem and 31; val c = k ushr 10
                        val v = fout.get(p * (32 * 32 * 8) + (h * 32 + w) * 8 + c)
                        if (v >= 0.5f) b = b or (1 shl (7 - bit))
                    }
                    out[base + byteI] = b.toByte()
                }
            }
            batch += bsz
        }
        return out
    }

    /** Self-test + bench : n patchs aléatoires, round-trip dec(enc(x)), renvoie un résumé. */
    @JvmStatic
    fun selfTest(n: Int): String {
        try {
            val rnd = java.util.Random(1)
            val patches = ByteArray(n * PATCH).also { rnd.nextBytes(it) }
            // warmup
            decode(encode(ByteArray(PATCH).also { rnd.nextBytes(it) }))
            var t = System.nanoTime()
            val lat = encode(patches)
            val encMs = (System.nanoTime() - t) / 1e6
            t = System.nanoTime()
            val back = decode(lat)
            val decMs = (System.nanoTime() - t) / 1e6
            var mism = 0
            for (i in 0 until n * PATCH) if (back[i] != patches[i]) { mism++ }
            val mb = n * 1024.0 / 1e6
            return "backend=$backendName n=$n enc=${"%.0f".format(encMs)}ms(${"%.1f".format(mb / (encMs / 1000))}Mo/s) " +
                "dec=${"%.0f".format(decMs)}ms(${"%.1f".format(mb / (decMs / 1000))}Mo/s) mism=$mism/${n * PATCH}o"
        } catch (e: Throwable) {
            return "selfTest error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }
}
