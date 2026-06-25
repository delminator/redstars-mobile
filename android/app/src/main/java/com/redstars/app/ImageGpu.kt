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
 * Inférence du DÉCODEUR image 256×256 via TFLite int8 sur NPU (delegate
 * GPU → NNAPI → CPU). Appelé depuis le helper Python (Chaquopy) sur Android.
 *
 * Modèle dec256.tflite (I/O VÉRIFIÉE, int8 PTQ) :
 *   INPUT  : int8 [1,32,32,4] NHWC, scale=0.0235294122248888 zp=0
 *            (latent fourni en NCHW : index = c*1024 + h*32 + w)
 *   OUTPUT : int8 [1,256,256,256] NHWC (C = 256 logits de classe palette),
 *            scale=0.21682730317115784 zp=4
 *   → argmax sur C donne l'indice palette ; l'argmax des logits int8 == argmax
 *     des logits déquantifiés, donc pas de déquant nécessaire.
 *
 * On quantifie l'entrée nous-mêmes (la quantification est bakée dans le modèle).
 * Le gros buffer de sortie 16 Mo est alloué UNE fois et réutilisé (évite le GC
 * par appel). Discipline rewind identique à CodecGpu (rewind output après run(),
 * rewind input avant remplissage).
 */
object ImageGpu {
    private const val IN_H = 32
    private const val IN_W = 32
    private const val IN_C = 4
    private const val OUT_H = 256
    private const val OUT_W = 256
    private const val OUT_C = 256
    private const val IN_SCALE = 0.0235294122248888
    private const val LATENT = 4096            // 4 * 1024
    private const val OUT_PIXELS = OUT_H * OUT_W   // 65536
    private const val OUT_BYTES = OUT_H * OUT_W * OUT_C  // 16_777_216

    // Encodeur (enc256.tflite) — int16 (activations int16, I/O = Short) :
    //   INPUT  : int16 [1,256,256,3] NHWC, scale=3.0518e-05 (=1/32768) zp=0
    //   OUTPUT : int16 [1,32,32,4]  NHWC, scale=9.1556e-05        zp=0
    private const val ENC_IN_SCALE = 3.0518e-05f
    private const val ENC_OUT_SCALE = 9.1556e-05f
    private const val ENC_IN_C = 3
    private const val ENC_IN_SHORTS = OUT_H * OUT_W * ENC_IN_C   // 256*256*3 = 196_608
    private const val ENC_OUT_SHORTS = IN_H * IN_W * IN_C        // 32*32*4   = 4096

    private var dec: Interpreter? = null
    private var enc: Interpreter? = null
    @Volatile private var backendI8 = "none"
    @Volatile private var backendEnc = "none"
    @Volatile private var loadError: String? = null

    private var inBuf: ByteBuffer? = null
    private var outBuf: ByteBuffer? = null
    private var encInBuf: ByteBuffer? = null
    private var encOutBuf: ByteBuffer? = null

    @JvmStatic
    @Synchronized
    fun init(ctx: Context) {
        if (dec != null || loadError != null) return
        try {
            val (interp, mode) = loadDec(loadAsset(ctx, "dec256.tflite"))
            if (interp == null) { loadError = "int8 : aucun backend TFLite n'a pu charger dec256.tflite"; return }
            dec = interp; backendI8 = mode
            inBuf = ByteBuffer.allocateDirect(IN_H * IN_W * IN_C).order(ByteOrder.nativeOrder())
            outBuf = ByteBuffer.allocateDirect(OUT_BYTES).order(ByteOrder.nativeOrder())
            // Encodeur int16 (best-effort) : si échec, encode juste indisponible.
            try {
                val (eInterp, eMode) = loadDec(loadAsset(ctx, "enc256.tflite"))
                if (eInterp != null) {
                    enc = eInterp; backendEnc = eMode
                    encInBuf = ByteBuffer.allocateDirect(ENC_IN_SHORTS * 2).order(ByteOrder.nativeOrder())
                    encOutBuf = ByteBuffer.allocateDirect(ENC_OUT_SHORTS * 2).order(ByteOrder.nativeOrder())
                } else backendEnc = "none"
            } catch (t: Throwable) { backendEnc = "absent" }
        } catch (e: Throwable) {
            loadError = "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    private fun loadDec(decBuf: MappedByteBuffer): Pair<Interpreter?, String> {
        for (mode in listOf("gpu", "nnapi", "cpu")) {
            try {
                val d = Interpreter(decBuf, optionsFor(mode))
                return Pair(d, mode)
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
        loadError?.let { "error: $it" } ?: "ok int8=$backendI8 enc=$backendEnc"

    private fun loadAsset(ctx: Context, name: String): MappedByteBuffer {
        ctx.assets.openFd(name).use { afd ->
            FileInputStream(afd.fileDescriptor).use { fis ->
                return fis.channel.map(FileChannel.MapMode.READ_ONLY, afd.startOffset, afd.declaredLength)
            }
        }
    }

    /**
     * latent : 4096 floats en ordre NCHW (index = c*1024 + h*32 + w, c∈0..3, h,w∈0..31),
     * valeurs ≈ [-3,3]. Retourne 65536 octets d'indices palette en row-major
     * (index = h*256 + w, h,w∈0..255).
     */
    @JvmStatic
    @Synchronized
    fun decodeImage(latent: FloatArray): ByteArray {
        val d = dec ?: throw IllegalStateException("ImageGpu non init (${status()})")
        val ib = inBuf ?: throw IllegalStateException("ImageGpu non init (${status()})")
        val ob = outBuf ?: throw IllegalStateException("ImageGpu non init (${status()})")

        // 1) Construire l'entrée int8 [1,32,32,4] NHWC (zp=0 → pas d'offset).
        ib.rewind()
        for (h in 0 until IN_H) {
            for (w in 0 until IN_W) {
                for (c in 0 until IN_C) {
                    val v = latent[c * 1024 + h * IN_W + w]
                    var q = Math.round(v / IN_SCALE).toInt()
                    if (q < -128) q = -128 else if (q > 127) q = 127
                    ib.put((h * IN_W + w) * IN_C + c, q.toByte())
                }
            }
        }

        // 2) Inférence ; rewind input avant, rewind output après.
        ib.rewind(); ob.rewind()
        d.run(ib, ob)
        ob.rewind()

        // 3) Argmax sur C (logits int8 signés). Pas de déquant nécessaire.
        val out = ByteArray(OUT_PIXELS)
        for (h in 0 until OUT_H) {
            for (w in 0 until OUT_W) {
                val base = (h * OUT_W + w) * OUT_C
                var best = ob.get(base).toInt()
                var bestC = 0
                for (c in 1 until OUT_C) {
                    val logit = ob.get(base + c).toInt()
                    if (logit > best) { best = logit; bestC = c }
                }
                out[h * OUT_W + w] = bestC.toByte()
            }
        }
        return out
    }

    /** Bench du décode image int8 sur n latents aléatoires : ms et images/s. */
    @JvmStatic
    fun benchImage(n: Int): String {
        try {
            dec ?: return "int8 dec non chargé (asset dec256.tflite manquant ?) — ${status()}"
            val rnd = java.util.Random(2)
            val lat = FloatArray(LATENT) { (rnd.nextFloat() * 6f) - 3f }
            decodeImage(lat)  // warmup
            val t = System.nanoTime()
            for (i in 0 until n) {
                for (k in 0 until LATENT) lat[k] = (rnd.nextFloat() * 6f) - 3f
                decodeImage(lat)
            }
            val ms = (System.nanoTime() - t) / 1e6
            val imgPerS = n / (ms / 1000)
            return "n=$n INT8[$backendI8] ${"%.0f".format(ms)}ms ${"%.2f".format(imgPerS)}img/s"
        } catch (e: Throwable) {
            return "benchImage error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * rgb : 256*256*3 octets uint8, row-major (pos (h,w,c) = (h*256+w)*3 + c,
     * c∈0..2 = R,G,B). Retourne 4096 floats = latent en NCHW
     * (index = c*1024 + h*32 + w, c∈0..3), valeurs ≈ [-3,3].
     */
    @JvmStatic
    @Synchronized
    fun encodeImage(rgb: ByteArray): FloatArray {
        val e = enc ?: throw IllegalStateException("ImageGpu encodeur non chargé (${status()})")
        val ib = encInBuf ?: throw IllegalStateException("ImageGpu encodeur non chargé (${status()})")
        val ob = encOutBuf ?: throw IllegalStateException("ImageGpu encodeur non chargé (${status()})")

        // 1) Entrée int16 [1,256,256,3] NHWC (zp=0 → pas d'offset).
        ib.rewind()
        val ibs = ib.asShortBuffer()
        for (h in 0 until OUT_H) {
            for (w in 0 until OUT_W) {
                for (c in 0 until ENC_IN_C) {
                    val v01 = (rgb[(h * OUT_W + w) * ENC_IN_C + c].toInt() and 0xFF) / 255f
                    var q = Math.round(v01 / ENC_IN_SCALE)
                    if (q < -32768) q = -32768 else if (q > 32767) q = 32767
                    ibs.put((h * OUT_W + w) * ENC_IN_C + c, q.toShort())
                }
            }
        }

        // 2) Inférence ; rewind input avant, rewind output après.
        ib.rewind(); ob.rewind()
        e.run(ib, ob)
        ob.rewind()

        // 3) Déquant + transpose NHWC→NCHW.
        val obs = ob.asShortBuffer()
        val latent = FloatArray(LATENT)
        for (c in 0 until IN_C) {
            for (h in 0 until IN_H) {
                for (w in 0 until IN_W) {
                    latent[c * 1024 + h * IN_W + w] =
                        obs.get((h * IN_W + w) * IN_C + c).toFloat() * ENC_OUT_SCALE
                }
            }
        }
        return latent
    }

    /** Bench de l'encode image int16 sur n images RGB aléatoires : ms et images/s. */
    @JvmStatic
    fun benchImageEncode(n: Int): String {
        try {
            enc ?: return "int16 enc non chargé (asset enc256.tflite manquant ?) — ${status()}"
            val rnd = java.util.Random(3)
            val rgb = ByteArray(ENC_IN_SHORTS).also { rnd.nextBytes(it) }
            encodeImage(rgb)  // warmup
            val t = System.nanoTime()
            for (i in 0 until n) {
                rnd.nextBytes(rgb)
                encodeImage(rgb)
            }
            val ms = (System.nanoTime() - t) / 1e6
            val imgPerS = n / (ms / 1000)
            return "n=$n INT16[$backendEnc] ${"%.0f".format(ms)}ms ${"%.2f".format(imgPerS)}img/s"
        } catch (e: Throwable) {
            return "benchImageEncode error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }
}
