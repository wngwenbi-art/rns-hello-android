package com.example.rnshello

import android.Manifest
import android.bluetooth.BluetoothManager
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.*

class MainActivity : AppCompatActivity() {

    private lateinit var spinnerDevices: Spinner
    private lateinit var btnConnect: Button
    private lateinit var tvMyAddress: TextView
    private lateinit var btnTabChat: Button
    private lateinit var btnTabAnnounces: Button
    private lateinit var panelChat: LinearLayout
    private lateinit var panelAnnounces: ScrollView
    private lateinit var scrollChat: ScrollView
    private lateinit var chatContainer: LinearLayout
    private lateinit var announcesContainer: LinearLayout
    private lateinit var etDestHash: EditText
    private lateinit var etMessage: EditText
    private lateinit var btnSend: Button

    private val handler = Handler(Looper.getMainLooper())
    private var refreshRunnable: Runnable? = null
    private var lastMessageCount = 0
    private var lastAnnounceCount = 0
    private val btService = BluetoothService()
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        spinnerDevices     = findViewById(R.id.spinnerDevices)
        btnConnect         = findViewById(R.id.btnConnect)
        tvMyAddress        = findViewById(R.id.tvMyAddress)
        btnTabChat         = findViewById(R.id.btnTabChat)
        btnTabAnnounces    = findViewById(R.id.btnTabAnnounces)
        panelChat          = findViewById(R.id.panelChat)
        panelAnnounces     = findViewById(R.id.panelAnnounces)
        scrollChat         = findViewById(R.id.scrollChat)
        chatContainer      = findViewById(R.id.chatContainer)
        announcesContainer = findViewById(R.id.announcesContainer)
        etDestHash         = findViewById(R.id.etDestHash)
        etMessage          = findViewById(R.id.etMessage)
        btnSend            = findViewById(R.id.btnSend)

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        btnTabChat.setOnClickListener { showTab("chat") }
        btnTabAnnounces.setOnClickListener { showTab("announces") }

        btnSend.setOnClickListener {
            val dest = etDestHash.text.toString().trim()
            val text = etMessage.text.toString().trim()
            if (dest.isEmpty()) { toast("Enter a destination address"); return@setOnClickListener }
            if (text.isEmpty()) { toast("Enter a message"); return@setOnClickListener }
            etMessage.setText("")
            scope.launch(Dispatchers.IO) {
                val result = RNSBridge.sendMessage(dest, text)
                withContext(Dispatchers.Main) { toast(result); refreshMessages() }
            }
        }

        requestPermissions()
    }

    private fun showTab(tab: String) {
        val cyan = android.content.res.ColorStateList.valueOf(Color.parseColor("#00d4ff"))
        val dark = android.content.res.ColorStateList.valueOf(Color.parseColor("#0f3460"))
        if (tab == "chat") {
            panelChat.visibility = View.VISIBLE
            panelAnnounces.visibility = View.GONE
            btnTabChat.backgroundTintList = cyan
            btnTabChat.setTextColor(Color.parseColor("#1a1a2e"))
            btnTabAnnounces.backgroundTintList = dark
            btnTabAnnounces.setTextColor(Color.WHITE)
        } else {
            panelChat.visibility = View.GONE
            panelAnnounces.visibility = View.VISIBLE
            btnTabAnnounces.backgroundTintList = cyan
            btnTabAnnounces.setTextColor(Color.parseColor("#1a1a2e"))
            btnTabChat.backgroundTintList = dark
            btnTabChat.setTextColor(Color.WHITE)
            refreshAnnounces()
        }
    }

    private fun startPolling() {
        refreshRunnable = object : Runnable {
            override fun run() {
                refreshMessages()
                refreshAnnounces()
                handler.postDelayed(this, 3000)
            }
        }
        handler.post(refreshRunnable!!)
    }

    private fun refreshMessages() {
        val messages = try { RNSBridge.getMessages() } catch (e: Exception) { return }
        if (messages.size == lastMessageCount) return
        lastMessageCount = messages.size
        runOnUiThread {
            chatContainer.removeAllViews()
            for (msg in messages) {
                addChatBubble(
                    msg["display_from"] ?: msg["from"] ?: "",
                    msg["text"] ?: "",
                    msg["ts"] ?: "",
                    msg["direction"] == "out",
                    (msg["from"] ?: "").replace("<", "").replace(">", "")
                )
            }
            scrollChat.post { scrollChat.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun refreshAnnounces() {
        val announces = try { RNSBridge.getAnnounces() } catch (e: Exception) { return }
        if (announces.size == lastAnnounceCount) return
        lastAnnounceCount = announces.size
        runOnUiThread {
            announcesContainer.removeAllViews()
            for (ann in announces.reversed()) {
                addAnnounceCard(ann["hash"] ?: "", ann["display"] ?: ann["name"] ?: "", ann["ts"] ?: "")
            }
        }
    }

    private fun addChatBubble(from: String, text: String, ts: String, isOutgoing: Boolean, rawHash: String = "") {
        val wrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(0, 4, 0, 4) }
            gravity = if (isOutgoing) Gravity.END else Gravity.START
        }

        if (!isOutgoing) {
            val senderLabel = TextView(this).apply {
                this.text = from
                textSize = 9f
                setTextColor(Color.parseColor("#00d4ff"))
                typeface = android.graphics.Typeface.MONOSPACE
            }
            if (rawHash.isNotEmpty()) {
                senderLabel.setOnLongClickListener {
                    val input = android.widget.EditText(this)
                    input.setText(RNSBridge.getContact(rawHash))
                    input.hint = "Enter nickname (blank to clear)"
                    androidx.appcompat.app.AlertDialog.Builder(this)
                        .setTitle("Set nickname")
                        .setMessage(rawHash)
                        .setView(input)
                        .setPositiveButton("Save") { _, _ ->
                            val nick = input.text.toString()
                            RNSBridge.setContact(rawHash, nick)
                            senderLabel.text = if (nick.isBlank()) rawHash else nick
                            toast(if (nick.isBlank()) "Nickname cleared" else "Saved: $nick")
                            lastMessageCount = 0  // force chat refresh
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                    true
                }
            }
            wrapper.addView(senderLabel)
        }

        wrapper.addView(TextView(this).apply {
            this.text = text
            textSize = 14f
            setTextColor(Color.WHITE)
            setPadding(16, 10, 16, 10)
            setBackgroundColor(if (isOutgoing) Color.parseColor("#0f3460") else Color.parseColor("#1a3a1a"))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { lp -> lp.gravity = if (isOutgoing) Gravity.END else Gravity.START }
        })

        wrapper.addView(TextView(this).apply {
            this.text = ts
            textSize = 9f
            setTextColor(Color.GRAY)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
        })

        chatContainer.addView(wrapper)
    }

    private fun addAnnounceCard(hash: String, name: String, ts: String) {
        val cleanHash = hash.replace("<", "").replace(">", "")
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(16, 12, 16, 12)
            setBackgroundColor(Color.parseColor("#0f3460"))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(0, 4, 0, 4) }
        }
        card.addView(TextView(this).apply {
            this.text = if (name.isNotEmpty()) name else "Unknown node"
            textSize = 14f
            setTextColor(Color.WHITE)
        })
        card.addView(TextView(this).apply {
            this.text = cleanHash
            textSize = 10f
            setTextColor(Color.parseColor("#00d4ff"))
            typeface = android.graphics.Typeface.MONOSPACE
        })
        card.addView(TextView(this).apply {
            this.text = "Seen at $ts"
            textSize = 9f
            setTextColor(Color.GRAY)
        })
        card.setOnClickListener {
            etDestHash.setText(cleanHash)
            showTab("chat")
            toast("Address copied - type a message and tap Send")
        }
        card.setOnLongClickListener {
            val input = android.widget.EditText(this)
            val currentName = RNSBridge.getContact(cleanHash)
            input.setText(currentName)
            input.hint = "Enter nickname (blank to clear)"
            androidx.appcompat.app.AlertDialog.Builder(this)
                .setTitle("Set nickname")
                .setMessage(cleanHash)
                .setView(input)
                .setPositiveButton("Save") { _, _ ->
                    val nick = input.text.toString()
                    RNSBridge.setContact(cleanHash, nick)
                    toast(if (nick.isBlank()) "Nickname cleared" else "Saved: $nick")
                    lastAnnounceCount = 0  // force UI refresh
                    refreshAnnounces()
                }
                .setNegativeButton("Cancel", null)
                .show()
            true
        }
        announcesContainer.addView(card)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    private fun requestPermissions() {
        val perms = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED) {
                perms.add(Manifest.permission.BLUETOOTH_CONNECT)
                perms.add(Manifest.permission.BLUETOOTH_SCAN)
            }
        }
        if (perms.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, perms.toTypedArray(), 1)
        } else {
            setupBluetooth()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            setupBluetooth()
        } else {
            toast("Bluetooth permissions denied!")
        }
    }

    private fun setupBluetooth() {
        val bm = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        val ba = bm.adapter ?: run { toast("No Bluetooth!"); return }

        val paired = ba.bondedDevices?.toList() ?: emptyList()
        toast("Found ${paired.size} paired device(s)")

        val names = paired.map { "${it.name} (${it.address})" }
        spinnerDevices.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_item, names
        ).also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }

        btnConnect.setOnClickListener {
            val idx = spinnerDevices.selectedItemPosition
            if (idx < 0 || idx >= paired.size) return@setOnClickListener
            val device = paired[idx]
            btnConnect.isEnabled = false
            toast("Connecting to ${device.address}...")

            scope.launch {
                val connected = withContext(Dispatchers.IO) {
                    btService.connect(device.address)
                }
                if (!connected) {
                    toast("BT connection failed")
                    btnConnect.isEnabled = true
                    return@launch
                }
                toast("BT connected. Starting RNS...")
                val addr = withContext(Dispatchers.IO) {
                    RNSBridge.start(btService)
                }
                if (addr.startsWith("Error")) {
                    toast("RNS error: $addr")
                    btnConnect.isEnabled = true
                } else {
                    val myAddr = addr
                    tvMyAddress.text = "My address: $myAddr"
                    tvMyAddress.setOnLongClickListener {
                        val input = android.widget.EditText(this@MainActivity)
                        input.setText(RNSBridge.getContact(myAddr))
                        input.hint = "Enter nickname for your address"
                        androidx.appcompat.app.AlertDialog.Builder(this@MainActivity)
                            .setTitle("Set my address nickname")
                            .setMessage(myAddr)
                            .setView(input)
                            .setPositiveButton("Save") { _, _ ->
                                val nick = input.text.toString()
                                RNSBridge.setContact(myAddr, nick)
                                tvMyAddress.text = if (nick.isBlank()) "My address: $myAddr"
                                                   else "My address: $nick"
                                toast(if (nick.isBlank()) "Nickname cleared" else "Saved: $nick")
                            }
                            .setNegativeButton("Cancel", null)
                            .show()
                        true
                    }
                    toast("Ready!")
                    startPolling()
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        refreshRunnable?.let { handler.removeCallbacks(it) }
        scope.cancel()
        btService.disconnect()
    }
}
