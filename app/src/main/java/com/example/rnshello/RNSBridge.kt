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

    // ── Contacts ──────────────────────────────────────────────────────────────

    fun saveContact(hashHex: String, name: String): String =
        worker.callAttr("save_contact", hashHex, name).toString()

    fun deleteContact(hashHex: String): String =
        worker.callAttr("delete_contact", hashHex).toString()

    fun getContacts(): List<Map<String, String>> =
        worker.callAttr("get_contacts").toStringMapList()

    fun resolveName(hashHex: String, fallback: String = ""): String =
        worker.callAttr("resolve_name", hashHex, fallback).toString()

    // ── RNode config ──────────────────────────────────────────────────────────

    /** Returns current radio config as map: frequency, bandwidth, txpower, sf, cr */
    fun getRnodeConfig(): Map<String, String> {
        val raw = worker.callAttr("get_rnode_config")
        return raw.asMap().entries.associate { (k, v) -> k.toString() to v.toString() }
    }

    /** Save new radio parameters. Returns "OK" or an error string. */
    fun saveRnodeConfig(
        frequency: Int, bandwidth: Int, txpower: Int, sf: Int, cr: Int
    ): String = worker.callAttr(
        "save_rnode_config", frequency, bandwidth, txpower, sf, cr
    ).toString()

    // ── Image sending ─────────────────────────────────────────────────────────

    /**
     * Send an image via RNS.Resource over a direct RNS.Link.
     * Bypasses LXMF — Resource handles sequencing, retransmit and integrity.
     * [webpBase64] is a base64-encoded WebP image (no data: prefix).
     * Blocks the calling thread until delivery or timeout (~120s).
     * Returns "Image sent (X KB)" or an error string.
     */
    fun sendImage(destHashHex: String, webpBase64: String): String =
        worker.callAttr("send_image", destHashHex, webpBase64).toString()

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun PyObject.toStringMapList(): List<Map<String, String>> =
        asList().map { item ->
            item.asMap().entries.associate { (k, v) -> k.toString() to v.toString() }
        }
}
