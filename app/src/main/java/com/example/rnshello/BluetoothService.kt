package com.example.rnshello

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothSocket
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

private val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
private const val TAG = "BluetoothService"

class BluetoothService {
    private var socket: BluetoothSocket? = null
    @Volatile var inputStream: InputStream? = null
    @Volatile var outputStream: OutputStream? = null

    @Volatile private var deviceAddress: String? = null
    @Volatile private var isConnected = false
    @Volatile private var reconnecting = false

    suspend fun connect(deviceAddress: String): Boolean = withContext(Dispatchers.IO) {
        this@BluetoothService.deviceAddress = deviceAddress
        connectInternal(deviceAddress)
    }

    private fun connectInternal(address: String): Boolean {
        return try {
            try { socket?.close() } catch (_: Exception) {}
            val adapter = BluetoothAdapter.getDefaultAdapter()
            val device = adapter.getRemoteDevice(address)
            val s = device.createRfcommSocketToServiceRecord(SPP_UUID)
            adapter.cancelDiscovery()
            s.connect()
            // Set a read timeout so read() never blocks indefinitely.
            // 2000ms: long enough to receive a full LoRa packet (118 bytes at
            // 1200 bps ≈ 800ms) but short enough that the drain loop and the
            // read loop can react to silence within a reasonable time.
            try { s.inputStream } // ensure stream is opened before setSoTimeout
            catch (_: Exception) {}
            try {
                // BluetoothSocket wraps a real socket — access via reflection
                val field = s.javaClass.getDeclaredField("mSocket")
                field.isAccessible = true
                val rawSocket = field.get(s) as? java.net.Socket
                rawSocket?.soTimeout = 2000  // 2 second read timeout
                Log.i(TAG, "BT socket read timeout set to 2000ms")
            } catch (e: Exception) {
                Log.w(TAG, "Could not set socket timeout (non-fatal): ${e.message}")
            }
            socket = s
            inputStream = s.inputStream
            outputStream = s.outputStream
            isConnected = true
            Log.i(TAG, "BT connected to $address")
            true
        } catch (e: Exception) {
            isConnected = false
            Log.e(TAG, "BT connect failed: ${e.message}")
            false
        }
    }

    // Reconnect runs fully async — never blocks the caller
    private fun triggerReconnect() {
        if (reconnecting) return
        reconnecting = true
        isConnected = false
        Thread {
            Log.i(TAG, "BT reconnect started...")
            val address = deviceAddress
            if (address == null) { reconnecting = false; return@Thread }
            var attempts = 0
            while (attempts < 20 && deviceAddress != null) {
                Thread.sleep(2000)
                if (connectInternal(address)) {
                    Log.i(TAG, "BT reconnected after ${attempts + 1} attempts")
                    break
                }
                attempts++
            }
            reconnecting = false
        }.also { it.isDaemon = true }.start()
    }

    fun read(maxBytes: Int): ByteArray {
        return try {
            val buf = ByteArray(maxBytes)
            val n = inputStream?.read(buf) ?: -1
            if (n <= 0) {
                Log.w(TAG, "BT read returned $n")
                triggerReconnect()
                ByteArray(0)
            } else {
                buf.copyOf(n)
            }
        } catch (e: java.net.SocketTimeoutException) {
            // Normal: no data arrived within the 2s timeout — not an error
            ByteArray(0)
        } catch (e: Exception) {
            Log.w(TAG, "BT read error: ${e.message}")
            triggerReconnect()
            ByteArray(0)
        }
    }

    /** Returns the number of bytes available to read without blocking. */
    fun available(): Int = try {
        inputStream?.available() ?: 0
    } catch (_: Exception) { 0 }

    // write() NEVER blocks for reconnect — just throws so Python logs it and moves on
    fun write(data: ByteArray) {
        if (!isConnected) {
            triggerReconnect()
            throw Exception("BT not connected, reconnecting...")
        }
        try {
            outputStream?.write(data)
        } catch (e: Exception) {
            Log.w(TAG, "BT write error: ${e.message}")
            triggerReconnect()
            throw e  // let Python layer log it, don't block here
        }
    }

    fun disconnect() {
        deviceAddress = null
        isConnected = false
        try { socket?.close() } catch (_: Exception) {}
    }
}
