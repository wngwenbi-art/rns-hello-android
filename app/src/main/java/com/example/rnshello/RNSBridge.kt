package com.example.rnshello

import com.chaquo.python.PyObject
import com.chaquo.python.Python

object RNSBridge {
    private val py = Python.getInstance()
    private val worker get() = py.getModule("rns_worker")

    fun start(btService: BluetoothService): String {
        // Wrap the Kotlin BluetoothService as a Python-callable object
        val pyBtWrapper = py.getModule("bt_wrapper").callAttr("BtWrapper", btService)
        return worker.callAttr("start", pyBtWrapper).toString()
    }

    fun sendMessage(destHashHex: String, text: String): String {
        return worker.callAttr("send_message", destHashHex, text).toString()
    }

    fun getAddress(): String {
        return worker.callAttr("get_address").toString()
    }

    fun getMessages(): List<Map<String, String>> {
        val raw = worker.callAttr("get_messages")
        val result = mutableListOf<Map<String, String>>()
        for (item in raw.asList()) {
            val map = mutableMapOf<String, String>()
            for ((k, v) in item.asMap()) {
                map[k.toString()] = v.toString()
            }
            result.add(map)
        }
        return result
    }

    fun announce(): String {
        return worker.callAttr("announce").toString()
    }

    fun getAnnounces(): List<Map<String, String>> {
        val raw = worker.callAttr("get_announces")
        val result = mutableListOf<Map<String, String>>()
        for (item in raw.asList()) {
            val map = mutableMapOf<String, String>()
            for ((k, v) in item.asMap()) {
                map[k.toString()] = v.toString()
            }
            result.add(map)
        }
        return result
    }
}
