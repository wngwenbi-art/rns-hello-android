package com.example.rnshello

import android.Manifest
import android.app.AlertDialog
import android.bluetooth.BluetoothManager
import android.content.pm.PackageManager
import android.graphics.Bitmap
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
import com.google.zxing.BarcodeFormat
import com.google.zxing.MultiFormatWriter
import com.google.zxing.integration.android.IntentIntegrator
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

    private var myAddress: String = ""

    // Chat filter state
    private var currentChatHash: String = ""
    private var chatFilterBanner: TextView? = null

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

        // Tap my address → show QR dialog
        tvMyAddress.setOnClickListener {
            if (myAddress.isNotEmpty()) showQrDialog(myAddress)
        }

        // Tap dest field → offer to scan QR
        etDestHash.setOnClickListener {
            AlertDialog.Builder(this)
                .setTitle("Enter address")
                .setMessage("Type address manually or scan a QR code")
                .setPositiveButton("📷 Scan QR") { _, _ -> launchQrScanner() }
                .setNegativeButton("Type manually", null)
                .show()
        }

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

        // Announce button
        val btnAnnounce = Button(this).apply {
            text = "📡 Announce"
            setTextColor(Color.parseColor("#1a1a2e"))
            backgroundTintList = android.content.res.ColorStateList.valueOf(Color.parseColor("#00d4ff"))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(8, 8, 8, 8) }
        }
        btnAnnounce.setOnClickListener {
            scope.launch(Dispatchers.IO) {
                val result = try { RNSBridge.announce() } catch (e: Exception) { "Error: ${e.message}" }
                withContext(Dispatchers.Main) { toast(result) }
            }
        }
        (btnTabAnnounces.parent as? android.view.ViewGroup)?.addView(btnAnnounce)

        requestPermissions()
    }

    // ── QR generation ────────────────────────────────────────────────────────

    private fun showQrDialog(address: String) {
        val size = 600
        val bits = try {
            MultiFormatWriter().encode(address, BarcodeFormat.QR_CODE, size, size)
        } catch (e: Exception) {
            toast("QR error: ${e.message}"); return
        }
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.RGB_565)
        for (x in 0 until size) for (y in 0 until size) {
            bmp.setPixel(x, y, if (bits[x, y]) Color.BLACK else Color.WHITE)
        }

        val iv = ImageView(this).apply {
            setImageBitmap(bmp)
            setPadding(32, 32, 32, 32)
        }

        AlertDialog.Builder(this)
            .setTitle("My Address")
            .setMessage(address)
            .setView(iv)
            .setPositiveButton("Copy") { _, _ ->
                val cm = getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager
                cm.setPrimaryClip(android.content.ClipData.newPlainText("address", address))
                toast("Address copied!")
            }
            .setNegativeButton("Close", null)
            .show()
    }

    // ── QR scanning ──────────────────────────────────────────────────────────

    private fun launchQrScanner() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
            return
        }
        IntentIntegrator(this).apply {
            setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
            setPrompt("Scan address QR code")
            setBeepEnabled(false)
            setOrientationLocked(false)
            initiateScan()
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: android.content.Intent?) {
        val result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (result != null) {
            if (result.contents != null) {
                val scanned = result.contents.trim()
                etDestHash.setText(scanned)
                showTab("chat")
                toast("Address scanned!")
            }
        } else {
            super.onActivityResult(requestCode, resultCode, data)
        }
    }

    // ── Tabs ─────────────────────────────────────────────────────────────────

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

    // ── Polling ───────────────────────────────────────────────────────────────

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
                    msg["from"] ?: "",
                    msg["text"] ?: "",
                    msg["ts"] ?: "",
                    msg["direction"] == "out"
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
                addAnnounceCard(ann["hash"] ?: "", ann["name"] ?: "", ann["ts"] ?: "")
            }
        }
    }

    // ── Chat bubbles ──────────────────────────────────────────────────────────

    private fun addChatBubble(from: String, text: String, ts: String, isOutgoing: Boolean) {
        val wrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(0, 4, 0, 4) }
            gravity = if (isOutgoing) Gravity.END else Gravity.START
        }

        if (!isOutgoing) {
            wrapper.addView(TextView(this).apply {
                this.text = from
                textSize = 9f
                setTextColor(Color.parseColor("#00d4ff"))
                typeface = android.graphics.Typeface.MONOSPACE
            })
        }

        val trimmedText = text.trim().trimStart('\u0000')
        if (trimmedText.startsWith("IMG:")) {
            try {
                val b64 = trimmedText.removePrefix("IMG:")
                val bytes = android.util.Base64.decode(b64, android.util.Base64.DEFAULT)
                val bmp = android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                wrapper.addView(ImageView(this).apply {
                    setImageBitmap(bmp)
                    layoutParams = LinearLayout.LayoutParams(300, LinearLayout.LayoutParams.WRAP_CONTENT)
                        .also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
                    adjustViewBounds = true
                })
            } catch (e: Exception) {
                wrapper.addView(TextView(this).apply { this.text = "[Image decode error]"; setTextColor(Color.RED) })
            }
        } else {
            wrapper.addView(TextView(this).apply {
                this.text = trimmedText
                textSize = 14f
                setTextColor(Color.WHITE)
                setPadding(16, 10, 16, 10)
                setBackgroundColor(if (isOutgoing) Color.parseColor("#0f3460") else Color.parseColor("#1a3a1a"))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).also { lp -> lp.gravity = if (isOutgoing) Gravity.END else Gravity.START }
            })
        }

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

    // ── Announce cards ────────────────────────────────────────────────────────

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
            toast("Address loaded — type a message and tap Send")
        }
        announcesContainer.addView(card)
    }

    // ── Permissions ───────────────────────────────────────────────────────────

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
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED) {
            perms.add(Manifest.permission.CAMERA)
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
        when (requestCode) {
            REQ_CAMERA -> if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) launchQrScanner()
            else -> if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) setupBluetooth()
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
                val connected = withContext(Dispatchers.IO) { btService.connect(device.address) }
                if (!connected) {
                    toast("BT connection failed")
                    btnConnect.isEnabled = true
                    return@launch
                }
                toast("BT connected. Starting RNS...")
                val addr = withContext(Dispatchers.IO) { RNSBridge.start(btService) }
                if (addr.startsWith("Error")) {
                    toast("RNS error: $addr")
                    btnConnect.isEnabled = true
                } else {
                    myAddress = addr
                    tvMyAddress.text = "📋 My address: $addr"
                    toast("Ready! Tap address to show QR")
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

    companion object {
        const val REQ_CAMERA = 101
    }
}
