package com.example.rnshello

import com.chaquo.python.Python

class RNSBridge(private val btService: BluetoothService) {
    private val py = Python.getInstance()
    private val worker = py.getModule("rns_worker")

    inner class SocketWrapper {
        fun read(n: Int): ByteArray = btService.read(n)
        fun write(data: ByteArray) = btService.write(data)
    }

    fun start(): String = worker.callAttr("start", SocketWrapper()).toString()
    fun sendHello(destHash: String): String = worker.callAttr("send_hello", destHash).toString()
    fun getAddress(): String = worker.callAttr("get_address").toString()
}
