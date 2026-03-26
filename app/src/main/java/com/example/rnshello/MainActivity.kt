package com.example.rnshello

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.view.Gravity
import android.view.View
import android.content.ClipData
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.MediaStore
import android.widget.*
import android.widget.ImageButton
import java.io.ByteArrayOutputStream
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.zxing.BarcodeFormat
import com.google.zxing.MultiFormatWriter
import androidx.core.content.FileProvider
import kotlinx.coroutines.*

class MainActivity : AppCompatActivity() {

    // ── Views ─────────────────────────────────────────────────────────────────

    private lateinit var spinnerDevices:     Spinner
    private lateinit var btnConnect:         Button
    private lateinit var tvMyAddress:        TextView
    private lateinit var btnTabChat:         Button
    private lateinit var btnTabAnnounces:    Button
    private lateinit var btnTabContacts:     Button
    private lateinit var panelChat:          LinearLayout
    private lateinit var panelAnnounces:     LinearLayout
    private lateinit var panelContacts:      LinearLayout
    private lateinit var scrollChat:         ScrollView
    private lateinit var chatContainer:      LinearLayout
    private lateinit var announcesContainer: LinearLayout
    private lateinit var contactsContainer:  LinearLayout
    private lateinit var etDestHash:         EditText
    private lateinit var etMessage:          EditText
    private lateinit var btnSend:            Button
    private lateinit var btnAnnounce:        Button
    private lateinit var btnSettings:        ImageButton
    private lateinit var btnAttach:          ImageButton

    // ── State ─────────────────────────────────────────────────────────────────

    private val handler           = Handler(Looper.getMainLooper())
    private var refreshRunnable:  Runnable? = null
    private var lastMessageCount  = 0
    private var lastAnnounceCount = 0
    private val btService         = BluetoothService()
    private val scope             = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var myAddress         = ""
    private var pendingImageBase64: String? = null  // set while image picked, cleared after send

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        bindViews()

        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        setupTabs()
        setupAddressBar()
        setupSendButton()
        setupAnnounceButton()
        setupSettingsButton()
        setupAttachButton()
        requestPermissions()
    }

    override fun onDestroy() {
        super.onDestroy()
        refreshRunnable?.let { handler.removeCallbacks(it) }
        scope.cancel()
        btService.disconnect()
    }

    // ── View binding ──────────────────────────────────────────────────────────

    private fun bindViews() {
        spinnerDevices     = findViewById(R.id.spinnerDevices)
        btnConnect         = findViewById(R.id.btnConnect)
        tvMyAddress        = findViewById(R.id.tvMyAddress)
        btnTabChat         = findViewById(R.id.btnTabChat)
        btnTabAnnounces    = findViewById(R.id.btnTabAnnounces)
        btnTabContacts     = findViewById(R.id.btnTabContacts)
        panelChat          = findViewById(R.id.panelChat)
        panelAnnounces     = findViewById(R.id.panelAnnounces)
        panelContacts      = findViewById(R.id.panelContacts)
        scrollChat         = findViewById(R.id.scrollChat)
        chatContainer      = findViewById(R.id.chatContainer)
        announcesContainer = findViewById(R.id.announcesContainer)
        contactsContainer  = findViewById(R.id.contactsContainer)
        etDestHash         = findViewById(R.id.etDestHash)
        etMessage          = findViewById(R.id.etMessage)
        btnSend            = findViewById(R.id.btnSend)
        btnAnnounce        = findViewById(R.id.btnAnnounce)
        btnSettings        = findViewById(R.id.btnSettings)
        btnAttach          = findViewById(R.id.btnAttach)
    }

    // ── UI setup ──────────────────────────────────────────────────────────────

    private fun setupTabs() {
        btnTabChat.setOnClickListener      { showTab("chat") }
        btnTabAnnounces.setOnClickListener { showTab("announces") }
        btnTabContacts.setOnClickListener  { showTab("contacts") }
    }

    private fun setupAddressBar() {
        // Tap my address → show QR
        tvMyAddress.setOnClickListener {
            if (myAddress.isNotEmpty()) showQrDialog(myAddress)
        }
        // Tap OR long-press dest field → same dialog: manual or scan QR
        val addressDialog = {
            AlertDialog.Builder(this)
                .setTitle("Enter address")
                .setMessage("Type address manually or scan a QR code")
                .setPositiveButton("📷 Scan QR") { dialog, _ ->
                    dialog.dismiss()
                    etDestHash.post { launchQrScanner() }
                }
                .setNegativeButton("Type manually", null)
                .show()
        }
        etDestHash.setOnClickListener     { addressDialog() }
        etDestHash.setOnLongClickListener { addressDialog(); true }
    }

    private fun setupSendButton() {
        btnSend.setOnClickListener {
            val dest = etDestHash.text.toString().trim()
            val text = etMessage.text.toString().trim()
            if (dest.isEmpty()) { toast("Enter a destination address"); return@setOnClickListener }
            if (text.isEmpty()) { toast("Enter a message");             return@setOnClickListener }
            etMessage.setText("")
            scope.launch(Dispatchers.IO) {
                val result = RNSBridge.sendMessage(dest, text)
                withContext(Dispatchers.Main) { toast(result); refreshMessages() }
            }
        }
    }

    private fun setupAnnounceButton() {
        btnAnnounce.setOnClickListener {
            scope.launch(Dispatchers.IO) {
                val result = try { RNSBridge.announce() } catch (e: Exception) { "Error: ${e.message}" }
                withContext(Dispatchers.Main) { toast(result) }
            }
        }
    }

    // ── Tabs ──────────────────────────────────────────────────────────────────

    private fun showTab(tab: String) {
        val cyan = colorStateList("#00d4ff")
        val dark = colorStateList("#0f3460")

        // Reset all tabs to dark, hide all panels
        listOf(btnTabChat, btnTabAnnounces, btnTabContacts).forEach {
            it.backgroundTintList = dark
            it.setTextColor(Color.WHITE)
        }
        panelChat.visibility      = View.GONE
        panelAnnounces.visibility = View.GONE
        panelContacts.visibility  = View.GONE

        when (tab) {
            "chat" -> {
                panelChat.visibility = View.VISIBLE
                btnTabChat.backgroundTintList = cyan
                btnTabChat.setTextColor(Color.parseColor("#1a1a2e"))
            }
            "announces" -> {
                panelAnnounces.visibility = View.VISIBLE
                btnTabAnnounces.backgroundTintList = cyan
                btnTabAnnounces.setTextColor(Color.parseColor("#1a1a2e"))
                refreshAnnounces()
            }
            "contacts" -> {
                panelContacts.visibility = View.VISIBLE
                btnTabContacts.backgroundTintList = cyan
                btnTabContacts.setTextColor(Color.parseColor("#1a1a2e"))
                refreshContacts()
            }
        }
    }

    // ── Bluetooth setup ───────────────────────────────────────────────────────

    private fun setupBluetooth() {
        val bm = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        val ba = bm.adapter ?: run { toast("No Bluetooth!"); return }

        val paired = ba.bondedDevices?.toList() ?: emptyList()
        toast("Found ${paired.size} paired device(s)")

        spinnerDevices.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            paired.map { "${it.name} (${it.address})" }
        ).also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }

        btnConnect.setOnClickListener {
            val idx = spinnerDevices.selectedItemPosition
            if (idx < 0 || idx >= paired.size) return@setOnClickListener
            btnConnect.isEnabled = false
            toast("Connecting to ${paired[idx].address}...")
            scope.launch { connectAndStart(paired[idx].address) }
        }
    }

    private suspend fun connectAndStart(address: String) {
        val connected = withContext(Dispatchers.IO) { btService.connect(address) }
        if (!connected) {
            toast("BT connection failed"); btnConnect.isEnabled = true; return
        }
        toast("BT connected. Starting RNS...")
        val addr = withContext(Dispatchers.IO) { RNSBridge.start(btService) }
        if (addr.startsWith("Error")) {
            toast("RNS error: $addr"); btnConnect.isEnabled = true
        } else {
            myAddress = addr
            tvMyAddress.text = "📋 My address: $addr"
            toast("Ready! Tap address to show QR")
            startPolling()
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
        val messages = try { RNSBridge.getMessages() } catch (_: Exception) { return }
        if (messages.size == lastMessageCount) return
        lastMessageCount = messages.size
        runOnUiThread {
            chatContainer.removeAllViews()
            messages.forEach { msg ->
                val hash = msg["from"] ?: ""
                // UI layer only: resolve hash → nickname, fall back to truncated hash
                val displayName = if (msg["direction"] == "out") "me"
                                  else RNSBridge.resolveName(hash, hash.take(8))
                addChatBubble(
                    from       = displayName,
                    text       = msg["text"] ?: "",
                    ts         = msg["ts"]   ?: "",
                    isOutgoing = msg["direction"] == "out"
                )
            }
            scrollChat.post { scrollChat.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun refreshAnnounces() {
        val announces = try { RNSBridge.getAnnounces() } catch (_: Exception) { return }
        if (announces.size == lastAnnounceCount) return
        lastAnnounceCount = announces.size
        runOnUiThread {
            announcesContainer.removeAllViews()
            announces.reversed().forEach { ann ->
                val hash    = ann["hash"] ?: ""
                val rnsName = ann["name"] ?: ""
                // Contact nickname takes priority over RNS announce name
                val displayName = RNSBridge.resolveName(hash, rnsName)
                addAnnounceCard(hash = hash, displayName = displayName, ts = ann["ts"] ?: "")
            }
        }
    }

    /**
     * Contacts tab: built from message history — every unique address
     * that has ever sent or received a message on this device.
     */
    private fun refreshContacts() {
        val messages = try { RNSBridge.getMessages() } catch (_: Exception) { return }
        // Collect unique peer hashes from message history (exclude "me")
        val seen = linkedSetOf<String>()
        messages.forEach { msg ->
            val hash = msg["from"] ?: ""
            if (hash.isNotEmpty() && hash != "me" && hash.length == 32) seen.add(hash)
        }
        runOnUiThread {
            contactsContainer.removeAllViews()
            if (seen.isEmpty()) {
                contactsContainer.addView(TextView(this).apply {
                    text = "No conversations yet.\nStart chatting and addresses will appear here."
                    setTextColor(Color.GRAY)
                    textSize = 14f
                    gravity = Gravity.CENTER
                    setPadding(32, 64, 32, 0)
                })
            } else {
                seen.forEach { hash -> addContactCard(hash) }
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
                textSize  = 9f
                setTextColor(Color.parseColor("#00d4ff"))
                typeface  = Typeface.MONOSPACE
            })
        }

        val trimmed = text.trim().trimStart('\u0000')
        wrapper.addView(when {
            trimmed.startsWith("IMG_FILE:") -> buildImageFileCard(trimmed.removePrefix("IMG_FILE:"), isOutgoing)
            trimmed.startsWith("IMG_B64:") || trimmed.startsWith("IMG:") -> buildImageBubble(trimmed, isOutgoing)
            else -> buildTextBubble(trimmed, isOutgoing)
        })

        wrapper.addView(TextView(this).apply {
            this.text = ts
            textSize  = 9f
            setTextColor(Color.GRAY)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
        })

        chatContainer.addView(wrapper)
    }

    private fun buildTextBubble(text: String, isOutgoing: Boolean): TextView =
        TextView(this).apply {
            this.text = text
            textSize  = 14f
            setTextColor(Color.WHITE)
            setPadding(16, 10, 16, 10)
            setBackgroundColor(
                if (isOutgoing) Color.parseColor("#0f3460")
                else            Color.parseColor("#1a3a1a")
            )
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
        }

    private fun buildImageBubble(trimmed: String, isOutgoing: Boolean): View {
        return try {
            // Support both IMG_B64: (LXMF attachments) and legacy IMG: prefix
            val b64 = when {
                trimmed.startsWith("IMG_B64:") -> trimmed.removePrefix("IMG_B64:")
                trimmed.startsWith("IMG:")     -> trimmed.removePrefix("IMG:")
                else -> trimmed
            }
            val bytes = Base64.decode(b64, Base64.NO_WRAP)
            val bmp   = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ImageView(this).apply {
                setImageBitmap(bmp)
                adjustViewBounds = true
                layoutParams = LinearLayout.LayoutParams(300, LinearLayout.LayoutParams.WRAP_CONTENT)
                    .also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
            }
        } catch (_: Exception) {
            TextView(this).apply {
                text = "[Image decode error]"
                setTextColor(Color.RED)
            }
        }
    }

    private fun buildImageFileCard(filePath: String, isOutgoing: Boolean): View {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(16, 12, 16, 12)
            setBackgroundColor(
                if (isOutgoing) Color.parseColor("#0f3460") else Color.parseColor("#1a3a1a")
            )
            gravity = android.view.Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.gravity = if (isOutgoing) android.view.Gravity.END else android.view.Gravity.START }
        }

        // Icon
        card.addView(TextView(this).apply {
            text = "🖼️"
            textSize = 28f
            setPadding(0, 0, 12, 0)
        })

        val info = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        info.addView(TextView(this).apply {
            text = if (isOutgoing) "Image sent" else "Image received"
            textSize = 13f
            setTextColor(Color.WHITE)
        })
        info.addView(TextView(this).apply {
            text = "Tap to open"
            textSize = 10f
            setTextColor(Color.parseColor("#00d4ff"))
        })
        card.addView(info)

        // Tap to open in gallery/viewer
        card.setOnClickListener {
            try {
                val file = java.io.File(filePath)
                if (!file.exists()) { toast("File not found: $filePath"); return@setOnClickListener }
                // Copy to external storage so viewer can access it
                val destDir = android.os.Environment.getExternalStoragePublicDirectory(
                    android.os.Environment.DIRECTORY_PICTURES)
                val destFile = java.io.File(destDir, file.name)
                destDir.mkdirs()
                file.copyTo(destFile, overwrite = true)
                // Trigger media scan
                sendBroadcast(android.content.Intent(
                    android.content.Intent.ACTION_MEDIA_SCANNER_SCAN_FILE,
                    android.net.Uri.fromFile(destFile)
                ))
                // Open with viewer
                val uri = androidx.core.content.FileProvider.getUriForFile(
                    this, "${packageName}.fileprovider", destFile)
                val intent = android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, "image/webp")
                    addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
                if (intent.resolveActivity(packageManager) != null) {
                    startActivity(intent)
                } else {
                    toast("No image viewer found")
                }
            } catch (e: Exception) {
                toast("Could not open image: ${e.message}")
            }
        }
        return card
    }

    // ── Announce cards ────────────────────────────────────────────────────────

    private fun addAnnounceCard(hash: String, displayName: String, ts: String) {
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
            text     = displayName.ifEmpty { "Unknown node" }
            textSize = 14f
            setTextColor(Color.WHITE)
        })
        card.addView(TextView(this).apply {
            text     = cleanHash
            textSize = 10f
            setTextColor(Color.parseColor("#00d4ff"))
            typeface = Typeface.MONOSPACE
        })
        card.addView(TextView(this).apply {
            text     = "Seen at $ts"
            textSize = 9f
            setTextColor(Color.GRAY)
        })
        // Tap → load into chat
        card.setOnClickListener {
            etDestHash.setText(cleanHash)
            showTab("chat")
            toast("Address loaded — type a message and tap Send")
        }
        announcesContainer.addView(card)
    }

    // ── Contact cards (Contacts tab) ──────────────────────────────────────────

    private fun addContactCard(hash: String) {
        val cleanHash = hash.replace("<", "").replace(">", "")
        // Resolve current nickname — fall back to truncated hash
        val nickname = RNSBridge.resolveName(cleanHash, "")
        val displayName = nickname.ifEmpty { "${cleanHash.take(8)}…${cleanHash.takeLast(4)}" }

        val card = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(16, 14, 16, 14)
            setBackgroundColor(Color.parseColor("#0f3460"))
            gravity = Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(0, 4, 0, 4) }
        }

        // Info column
        val info = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        info.addView(TextView(this).apply {
            text     = displayName
            textSize = 15f
            setTextColor(Color.WHITE)
        })
        info.addView(TextView(this).apply {
            text     = "${cleanHash.take(8)}…${cleanHash.takeLast(4)}"
            textSize = 10f
            setTextColor(Color.parseColor("#00d4ff"))
            typeface = Typeface.MONOSPACE
        })
        card.addView(info)

        // Chat button
        card.addView(Button(this).apply {
            text = "💬"
            textSize = 16f
            backgroundTintList = colorStateList("#00d4ff")
            setTextColor(Color.parseColor("#1a1a2e"))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.marginStart = 8 }
            setOnClickListener {
                etDestHash.setText(cleanHash)
                showTab("chat")
            }
        })

        // Tap card → load into chat
        card.setOnClickListener {
            etDestHash.setText(cleanHash)
            showTab("chat")
        }

        // Long press card → contact card dialog to assign / edit nickname
        card.setOnLongClickListener {
            showContactCardDialog(cleanHash, nickname)
            true
        }

        contactsContainer.addView(card)
    }

    // ── Contact card dialog ───────────────────────────────────────────────────

    /**
     * Shows full address + nickname input.
     * Saving updates the nickname used everywhere in the UI.
     * Called from: long-press on a contact card in the Contacts tab.
     */
    private fun showContactCardDialog(hash: String, currentNickname: String) {
        val cleanHash = hash.replace("<", "").replace(">", "")

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 8)
        }

        // Full address — always visible
        layout.addView(TextView(this).apply {
            text     = cleanHash
            textSize = 11f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.parseColor("#00d4ff"))
            setPadding(0, 0, 0, 16)
        })

        layout.addView(TextView(this).apply {
            text     = "Nickname"
            textSize = 12f
            setTextColor(Color.GRAY)
        })

        val input = EditText(this).apply {
            setText(currentNickname)
            hint = "e.g. Alice's RNode"
            setTextColor(Color.parseColor("#1a1a2e"))      // dark text on white dialog background
            setHintTextColor(Color.parseColor("#999999"))  // mid-grey hint, still readable
            setSingleLine(true)
        }
        layout.addView(input)

        AlertDialog.Builder(this)
            .setTitle("Contact card")
            .setView(layout)
            .setPositiveButton("💾 Save") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isEmpty()) { toast("Enter a nickname"); return@setPositiveButton }
                scope.launch(Dispatchers.IO) {
                    RNSBridge.saveContact(cleanHash, name)
                    withContext(Dispatchers.Main) {
                        toast("Saved: $name")
                        // Force re-render everywhere so new name appears immediately
                        lastMessageCount  = 0
                        lastAnnounceCount = 0
                        refreshMessages()
                        refreshAnnounces()
                        refreshContacts()
                    }
                }
            }
            .setNegativeButton("Cancel", null)
            .also { builder ->
                if (currentNickname.isNotEmpty()) {
                    builder.setNeutralButton("🗑️ Remove") { _, _ ->
                        scope.launch(Dispatchers.IO) {
                            RNSBridge.deleteContact(cleanHash)
                            withContext(Dispatchers.Main) {
                                toast("Nickname removed")
                                lastMessageCount  = 0
                                lastAnnounceCount = 0
                                refreshMessages()
                                refreshAnnounces()
                                refreshContacts()
                            }
                        }
                    }
                }
            }
            .show()
    }

    // ── Image attach ─────────────────────────────────────────────────────────

    private fun setupAttachButton() {
        btnAttach.setOnClickListener {
            val dest = etDestHash.text.toString().trim()
            if (dest.isEmpty()) { toast("Enter a destination address first"); return@setOnClickListener }
            showImageSourceDialog()
        }
    }

    private fun showImageSourceDialog() {
        AlertDialog.Builder(this)
            .setTitle("Send image")
            .setItems(arrayOf("📷 Take photo", "🖼️ Choose from gallery")) { _, which ->
                when (which) {
                    0 -> launchImageCamera()
                    1 -> launchImageGallery()
                }
            }
            .show()
    }

    private fun launchImageCamera() {
        if (!hasPermission(Manifest.permission.CAMERA)) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
            return
        }
        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
        if (intent.resolveActivity(packageManager) != null) {
            startActivityForResult(intent, REQ_IMAGE_CAMERA)
        } else {
            toast("No camera app available")
        }
    }

    private fun launchImageGallery() {
        val intent = Intent(Intent.ACTION_PICK, MediaStore.Images.Media.EXTERNAL_CONTENT_URI)
        intent.type = "image/*"
        startActivityForResult(intent, REQ_IMAGE_GALLERY)
    }

    /**
     * Compress a bitmap for LoRa radio transmission.
     * Target: ≤ 4 KB so the image fits within a single LXMF link transfer
     * at SF8 / 31.25 kHz (≈ 1200 baud effective throughput).
     *
     * Strategy:
     *   - Scale longest side to 180 px  (enough detail for a thumbnail)
     *   - Encode as WebP at quality 12  (aggressive but still recognisable)
     *   - If result > 4 KB, re-encode at quality 5 as a safety net
     *
     * Typical output: 1.5–3.5 KB.
     */
    private fun compressToBase64(bmp: android.graphics.Bitmap): String {
        val maxDim = 180
        val scaled = if (bmp.width > maxDim || bmp.height > maxDim) {
            val ratio = maxDim.toFloat() / maxOf(bmp.width, bmp.height)
            android.graphics.Bitmap.createScaledBitmap(
                bmp,
                (bmp.width * ratio).toInt().coerceAtLeast(1),
                (bmp.height * ratio).toInt().coerceAtLeast(1),
                true
            )
        } else bmp

        var out = ByteArrayOutputStream()
        scaled.compress(android.graphics.Bitmap.CompressFormat.WEBP, 12, out)

        // Safety net: if still > 4 KB, crush harder
        if (out.size() > 4 * 1024) {
            out = ByteArrayOutputStream()
            scaled.compress(android.graphics.Bitmap.CompressFormat.WEBP, 5, out)
        }

        val bytes = out.toByteArray()
        val kb = bytes.size / 1024f
        android.util.Log.d("RNSHello", "Image compressed: ${"%.1f".format(kb)} KB (WebP @ ${scaled.width}×${scaled.height})")
        return android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
    }

    /** Estimate LoRa transfer time: 1200 baud effective, 8-bit bytes, ~25% protocol overhead. */
    private fun loraEstimateSecs(kb: Int): Int = ((kb * 1024 * 8 * 1.25) / 1200).toInt().coerceAtLeast(5)

    private fun handleImageResult(bmp: android.graphics.Bitmap) {
        val dest = etDestHash.text.toString().trim()
        if (dest.isEmpty()) { toast("No destination set"); return }

        toast("Compressing image…")
        scope.launch(Dispatchers.IO) {
            val b64 = compressToBase64(bmp)
            val kb  = (b64.length * 3 / 4) / 1024
            val estSecs = loraEstimateSecs(kb)
            withContext(Dispatchers.Main) {
                val preview = ImageView(this@MainActivity).apply {
                    val previewBytes = android.util.Base64.decode(b64, android.util.Base64.NO_WRAP)
                    setImageBitmap(BitmapFactory.decodeByteArray(previewBytes, 0, previewBytes.size))
                    adjustViewBounds = true
                    setPadding(16, 16, 16, 8)
                }
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Send image? (~${kb} KB)")
                    .setMessage("Estimated transfer time: ~${estSecs}s at 1200 baud LoRa.\nKeep both devices close with radio on.")
                    .setView(preview)
                    .setPositiveButton("📤 Send") { _, _ ->
                        scope.launch(Dispatchers.IO) {
                            val result = RNSBridge.sendImage(dest, b64)
                            withContext(Dispatchers.Main) {
                                toast(result)
                                // Refresh immediately so the outgoing bubble appears
                                lastMessageCount = 0
                                refreshMessages()
                            }
                        }
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
        }
    }

    // ── Settings ─────────────────────────────────────────────────────────────

    private fun setupSettingsButton() {
        btnSettings.setOnClickListener { showSettingsDialog() }
    }

    private fun showSettingsDialog() {
        val cfg = try { RNSBridge.getRnodeConfig() } catch (_: Exception) { emptyMap() }

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 8)
        }

        fun addRow(label: String, value: String, hint: String): EditText {
            layout.addView(TextView(this).apply {
                text      = label
                textSize  = 12f
                setTextColor(Color.GRAY)
                setPadding(0, 12, 0, 2)
            })
            return EditText(this).apply {
                setText(value)
                this.hint = hint
                setTextColor(Color.parseColor("#1a1a2e"))
                setHintTextColor(Color.parseColor("#999999"))
                setSingleLine(true)
                inputType = android.text.InputType.TYPE_CLASS_NUMBER
                layout.addView(this)
            }
        }

        val etFreq  = addRow("Frequency (Hz)",    cfg["frequency"] ?: "433025000", "e.g. 433025000")
        val etBw    = addRow("Bandwidth (Hz)",    cfg["bandwidth"]  ?: "31250",     "e.g. 31250")
        val etTx    = addRow("TX Power (0–17 dBm)", cfg["txpower"] ?: "17",        "0–17")
        val etSf    = addRow("Spreading Factor (6–12)", cfg["sf"]  ?: "8",         "6–12")
        val etCr    = addRow("Coding Rate (5–8)",  cfg["cr"]        ?: "6",        "5–8  (4/5 to 4/8)")

        // Helper note
        layout.addView(TextView(this).apply {
            text     = "Changes apply on next Connect."
            textSize = 11f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, 16, 0, 0)
        })

        AlertDialog.Builder(this)
            .setTitle("⚙️ RNode Radio Settings")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                try {
                    val freq = etFreq.text.toString().trim().toInt()
                    val bw   = etBw.text.toString().trim().toInt()
                    val tx   = etTx.text.toString().trim().toInt()
                    val sf   = etSf.text.toString().trim().toInt()
                    val cr   = etCr.text.toString().trim().toInt()
                    scope.launch(Dispatchers.IO) {
                        val result = RNSBridge.saveRnodeConfig(freq, bw, tx, sf, cr)
                        withContext(Dispatchers.Main) {
                            if (result == "OK") toast("Settings saved — reconnect to apply")
                            else toast(result)
                        }
                    }
                } catch (_: NumberFormatException) {
                    toast("All fields must be numbers")
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ── QR ────────────────────────────────────────────────────────────────────

    private fun showQrDialog(address: String) {
        val size = 600
        val bits = try {
            MultiFormatWriter().encode(address, BarcodeFormat.QR_CODE, size, size)
        } catch (e: Exception) { toast("QR error: ${e.message}"); return }
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.RGB_565).apply {
            for (x in 0 until size) for (y in 0 until size)
                setPixel(x, y, if (bits[x, y]) Color.BLACK else Color.WHITE)
        }
        AlertDialog.Builder(this)
            .setTitle("My Address")
            .setMessage(address)
            .setView(ImageView(this).apply { setImageBitmap(bmp); setPadding(32, 32, 32, 32) })
            .setPositiveButton("Copy") { _, _ ->
                val cm = getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager
                cm.setPrimaryClip(android.content.ClipData.newPlainText("address", address))
                toast("Address copied!")
            }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun launchQrScanner() {
        if (!hasPermission(Manifest.permission.CAMERA)) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
            return
        }
        startActivityForResult(Intent(this, QrScanActivity::class.java), REQ_QR_SCAN)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        when {
            requestCode == REQ_QR_SCAN && resultCode == Activity.RESULT_OK -> {
                val scanned = data?.getStringExtra("SCAN_RESULT")?.trim() ?: return
                etDestHash.setText(scanned)
                showTab("chat")
                toast("Address scanned!")
            }
            requestCode == REQ_IMAGE_CAMERA && resultCode == Activity.RESULT_OK -> {
                val bmp = data?.extras?.get("data") as? android.graphics.Bitmap
                if (bmp != null) {
                    handleImageResult(bmp)
                } else {
                    toast("Could not get image from camera")
                }
            }
            requestCode == REQ_IMAGE_GALLERY && resultCode == Activity.RESULT_OK -> {
                val uri: Uri = data?.data ?: return
                try {
                    val stream = contentResolver.openInputStream(uri)
                    val bmp = BitmapFactory.decodeStream(stream)
                    stream?.close()
                    if (bmp != null) handleImageResult(bmp)
                    else toast("Could not decode image")
                } catch (e: Exception) {
                    toast("Gallery error: ${e.message}")
                }
            }
            else -> super.onActivityResult(requestCode, resultCode, data)
        }
    }

    // ── Permissions ───────────────────────────────────────────────────────────

    private fun requestPermissions() {
        val perms = buildList {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                if (!hasPermission(Manifest.permission.BLUETOOTH_CONNECT)) {
                    add(Manifest.permission.BLUETOOTH_CONNECT)
                    add(Manifest.permission.BLUETOOTH_SCAN)
                }
            }
            if (!hasPermission(Manifest.permission.CAMERA))
                add(Manifest.permission.CAMERA)
        }
        if (perms.isEmpty()) setupBluetooth()
        else ActivityCompat.requestPermissions(this, perms.toTypedArray(), REQ_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        when (requestCode) {
            REQ_CAMERA      -> if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) launchQrScanner()
            REQ_PERMISSIONS -> if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) setupBluetooth()
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun hasPermission(perm: String) =
        ContextCompat.checkSelfPermission(this, perm) == PackageManager.PERMISSION_GRANTED

    private fun colorStateList(hex: String) =
        android.content.res.ColorStateList.valueOf(Color.parseColor(hex))

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    companion object {
        const val REQ_CAMERA       = 101
        const val REQ_QR_SCAN      = 102
        const val REQ_PERMISSIONS  = 1
        const val REQ_IMAGE_CAMERA = 103
        const val REQ_IMAGE_GALLERY= 104
    }
}
