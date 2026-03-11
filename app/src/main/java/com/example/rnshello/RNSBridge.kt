package com.example.rnshello

import com.chaquo.python.PyObject
import com.chaquo.python.Python

object RNSBridge {
    private val py = Python.getInstance()
    private val worker get() = py.getModule("rns_worker")

    fun start(socketWrapper: PyObject): String {
        return worker.callAttr("start", socketWrapper).toString()
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
        for (i in 0 until raw.asList().size) {
            val item = raw.asList()[i].asMap()
            val map = mutableMapOf<String, String>()
            for ((k, v) in item) {
                map[k.toString()] = v.toString()
            }
            result.add(map)
        }
        return result
    }

    fun getAnnounces(): List<Map<String, String>> {
        val raw = worker.callAttr("get_announces")
        val result = mutableListOf<Map<String, String>>()
        for (i in 0 until raw.asList().size) {
            val item = raw.asList()[i].asMap()
            val map = mutableMapOf<String, String>()
            for ((k, v) in item) {
                map[k.toString()] = v.toString()
            }
            result.add(map)
        }
        return result
    }
}
