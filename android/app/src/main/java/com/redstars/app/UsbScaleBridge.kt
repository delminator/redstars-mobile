package com.redstars.app

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import android.util.Log
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Reads the electronic scale (RS232 over a USB-serial adapter — CH340 & friends)
 * on Android and feeds the freshest weight line to the Python helper.
 *
 * WHY native: an Android app cannot open /dev/ttyUSB0 — USB access is gated behind
 * the Java USB Host API, so helper.py/Chaquopy (pyserial) can't reach the scale
 * directly. This bridge is the ONLY native part; helper.py stays the brain
 * (parsing + serving /helper/scale). We keep the transport dumb: read serial
 * lines, write the latest one (with a timestamp) to REDSTARS_SCALE_FILE. helper.py
 * reads that file on Android instead of the serial port.
 *
 * File format: one line "<epochMillis>\t<raw scale line>". helper.py treats the
 * reading as connected only while the timestamp is fresh.
 */
object UsbScaleBridge {
    private const val TAG = "UsbScaleBridge"
    private const val ACTION_USB_PERMISSION = "com.redstars.app.USB_PERMISSION"
    private const val BAUD = 9600

    private val running = AtomicBoolean(false)
    @Volatile private var outFile: File? = null

    /** Start the background reader once. Idempotent. `out` = REDSTARS_SCALE_FILE. */
    fun start(ctx: Context, out: File) {
        if (running.getAndSet(true)) return
        outFile = out
        try { out.parentFile?.mkdirs() } catch (_: Throwable) {}
        val appCtx = ctx.applicationContext
        Thread {
            try { supervise(appCtx) }
            catch (e: Throwable) { Log.e(TAG, "bridge died", e) }
            finally { running.set(false) }
        }.apply { isDaemon = true; name = "usb-scale-bridge"; start() }
    }

    /** Reconnect loop: find the adapter, get permission, stream until it drops. */
    private fun supervise(ctx: Context) {
        val manager = ctx.getSystemService(Context.USB_SERVICE) as UsbManager
        registerPermissionReceiver(ctx)
        while (running.get()) {
            try {
                val driver = UsbSerialProber.getDefaultProber().findAllDrivers(manager).firstOrNull()
                if (driver == null) { Thread.sleep(2500); continue }
                val device = driver.device
                if (!manager.hasPermission(device)) {
                    requestPermission(ctx, manager, device)
                    var waited = 0
                    while (running.get() && !manager.hasPermission(device) && waited < 30_000) {
                        Thread.sleep(500); waited += 500
                    }
                    if (!manager.hasPermission(device)) { Thread.sleep(3000); continue }
                }
                val connection = manager.openDevice(device)
                if (connection == null) { Thread.sleep(2500); continue }
                val port = driver.ports.firstOrNull()
                if (port == null) { connection.close(); Thread.sleep(2500); continue }
                Log.i(TAG, "scale opened: ${device.deviceName} vid=${device.vendorId} pid=${device.productId}")
                try {
                    port.open(connection)
                    port.setParameters(BAUD, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
                    try { port.setDTR(true); port.setRTS(true) } catch (_: Throwable) {}
                    readLines(port)
                } finally {
                    try { port.close() } catch (_: Throwable) {}
                    try { connection.close() } catch (_: Throwable) {}
                }
            } catch (e: Throwable) {
                Log.w(TAG, "scale read cycle error", e)
            }
            if (running.get()) Thread.sleep(2000) // device gone / error → retry
        }
    }

    /** Drain serial bytes, split on newline, persist the freshest line. */
    private fun readLines(port: UsbSerialPort) {
        val buf = ByteArray(256)
        val sb = StringBuilder()
        while (running.get()) {
            val n = try { port.read(buf, 600) } catch (e: Throwable) { Log.w(TAG, "read err", e); break }
            if (n <= 0) continue
            sb.append(String(buf, 0, n, Charsets.US_ASCII))
            var idx = indexOfNewline(sb)
            while (idx >= 0) {
                val line = sb.substring(0, idx).trim()
                sb.delete(0, idx + 1)
                if (line.isNotEmpty()) writeLine(line)
                idx = indexOfNewline(sb)
            }
            if (sb.length > 4096) sb.setLength(0) // runaway guard (no newlines)
        }
    }

    private fun indexOfNewline(sb: StringBuilder): Int {
        for (i in sb.indices) if (sb[i] == '\n' || sb[i] == '\r') return i
        return -1
    }

    private fun writeLine(line: String) {
        val f = outFile ?: return
        try {
            val tmp = File(f.parentFile, f.name + ".tmp")
            tmp.writeText("${System.currentTimeMillis()}\t$line\n", Charsets.US_ASCII)
            if (!tmp.renameTo(f)) { f.writeText("${System.currentTimeMillis()}\t$line\n", Charsets.US_ASCII); tmp.delete() }
        } catch (e: Throwable) { Log.w(TAG, "writeLine failed", e) }
    }

    private fun requestPermission(ctx: Context, manager: UsbManager, device: UsbDevice) {
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        val intent = Intent(ACTION_USB_PERMISSION).setPackage(ctx.packageName)
        val pi = PendingIntent.getBroadcast(ctx, 0, intent, flags)
        try { manager.requestPermission(device, pi) } catch (e: Throwable) { Log.w(TAG, "requestPermission failed", e) }
    }

    /** No-op receiver so the permission-result broadcast has a target; the loop
     *  polls hasPermission() rather than reacting here. */
    @Volatile private var receiverRegistered = false
    private fun registerPermissionReceiver(ctx: Context) {
        if (receiverRegistered) return
        receiverRegistered = true
        val receiver = object : BroadcastReceiver() { override fun onReceive(c: Context?, i: Intent?) {} }
        val filter = IntentFilter(ACTION_USB_PERMISSION)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                ctx.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                ctx.registerReceiver(receiver, filter)
            }
        } catch (e: Throwable) { Log.w(TAG, "receiver register failed", e) }
    }
}
