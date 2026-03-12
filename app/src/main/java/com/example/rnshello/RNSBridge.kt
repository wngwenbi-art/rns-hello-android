package com.example.rnshello

import com.chaquo.python.PyObject
import com.chaquo.python.Python

object RNSBridge {

    private val py     = Python.getInstance()
    private val worker get() = py.getModule("rns_worker")

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    fun start(btService: BluetoothService): String {
        val pyBtWrapper = py.getModule("bt_wrapper").callAttr("BtWrapper", btService)
        return worker.callAttr("start", pyBtWrapper).toString()
    }

    fun announce(): String =
        worker.callAttr("announce").toString()

    fun getAddress(): String =
        worker.callAttr("get_address").toString()

    // ── Messaging ─────────────────────────────────────────────────────────────

    fun sendMessage(destHashHex: String, text: String): String =
        worker.callAttr("send_message", destHashHex, text).toString()

    fun getMessages(): List<Map<String, String>> =
        worker.callAttr("get_messages").toStringMapList()

    fun getAnnounces(): List<Map<String, String>> =
        worker.callAttr("get_announces").toStringMapList()

    // ── Helpers ───────────────────────────────────────────────────────────────

    /** Converts a Python list-of-dicts into a Kotlin List<Map<String, String>>. */
    private fun PyObject.toStringMapList(): List<Map<String, String>> =
        asList().map { item ->
            item.asMap().entries.associate { (k, v) -> k.toString() to v.toString() }
        }
}
