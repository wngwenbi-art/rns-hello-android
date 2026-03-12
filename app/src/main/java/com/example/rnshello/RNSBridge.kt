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

    /** Save or update a contact name for a given hash. */
    fun saveContact(hashHex: String, name: String): String =
        worker.callAttr("save_contact", hashHex, name).toString()

    /** Delete a contact by hash. */
    fun deleteContact(hashHex: String): String =
        worker.callAttr("delete_contact", hashHex).toString()

    /** Return all saved contacts as [{hash, name}] */
    fun getContacts(): List<Map<String, String>> =
        worker.callAttr("get_contacts").toStringMapList()

    /**
     * Resolve a hash to a friendly name at the UI layer only.
     * RNS operations always use the raw hash — never this.
     * [fallback] is typically the RNS announce display name.
     */
    fun resolveName(hashHex: String, fallback: String = ""): String =
        worker.callAttr("resolve_name", hashHex, fallback).toString()

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun PyObject.toStringMapList(): List<Map<String, String>> =
        asList().map { item ->
            item.asMap().entries.associate { (k, v) -> k.toString() to v.toString() }
        }
}
