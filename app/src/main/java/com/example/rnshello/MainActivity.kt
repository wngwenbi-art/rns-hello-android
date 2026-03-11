package com.example.rnshello

import android.Manifest
import android.bluetooth.BluetoothAdapter
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        spinnerDevices    = findViewById(R.id.spinnerDevices)
        btnConnect        = findViewById(R.id.btnConnect)
        tvMyAddress       = findViewById(R.id.tvMyAddress)
        btnTabChat        = findViewById(R.id.btnTabChat)
        btnTabAnnounces   = findViewById(R.id.btnTabAnnounces)
        panelChat         = findViewById(R.id.panelChat)
        panelAnnounces    = findViewById(R.id.panelAnnounces)
        scrollChat        = findViewById(R.id.scrollChat)
        chatContainer     = findViewById(R.id.chatContainer)
        announcesContainer = findViewById(R.id.announcesContainer)
        etDestHash        = findViewById(R.id.etDestHash)
        etMessage         = findViewById(R.id.etMessage)
        btnSend           = findViewById(R.id.btnSend)

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        btnTabChat.setOnClickListener { showTab("chat") }
        btnTabAnnounces.setOnClickListener { showTab("announces") }

        btnSend.setOnClickListener {
            val dest = etDestHash.text.toString().trim()
            val text = etMessage.text.toString().trim()
            if (dest.isEmpty()) {
                toast("Enter a destination address")
                return@setOnClickListener
            }
            if (text.isEmpty()) {
                toast("Enter a message")
                return@setOnClickListener
            }
            etMessage.setText("")
            Thread {
                val result = RNSBridge.sendMessage(dest, text)
                runOnUiThread {
                    toast(result)
                    refreshMessages()
                }
            }.start()
        }

        requestPermissions()
    }

    private fun showTab(tab: String) {
        if (tab == "chat") {
            panelChat.visibility = View.VISIBLE
            panelAnnounces.visibility = View.GONE
            btnTabChat.backgroundTintList = android.content.res.ColorStateList.valueOf(Color.parseColor("#00d4ff"))
            btnTabChat.setTextColor(Color.parseColor("#1a1a2e"))
            btnTabAnnounces.backgroundTintList = android.content.res.ColorStateList.valueOf(Color.parseColor("#0f3460"))
            btnTabAnnounces.setTextColor(Color.WHITE)
        } else {
            panelChat.visibility = View.GONE
            panelAnnounces.visibility = View.VISIBLE
            btnTabAnnounces.backgroundTintList = android.content.res.ColorStateList.valueOf(Color.parseColor("#00d4ff"))
            btnTabAnnounces.setTextColor(Color.parseColor("#1a1a2e"))
            btnTabChat.backgroundTintList = android.content.res.ColorStateList.valueOf(Color.parseColor("#0f3460"))
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
        val messages = RNSBridge.getMessages()
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
        val announces = RNSBridge.getAnnounces()
        if (announces.size == lastAnnounceCount) return
        lastAnnounceCount = announces.size
        runOnUiThread {
            announcesContainer.removeAllViews()
            for (ann in announces.reversed()) {
                addAnnounceCard(
                    ann["hash"] ?: "",
                    ann["name"] ?: "",
                    ann["ts"] ?: ""
                )
            }
        }
    }

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
            val fromTv = TextView(this).apply {
                this.text = from
                textSize = 9f
                setTextColor(Color.parseColor("#00d4ff"))
                typeface = android.graphics.Typeface.MONOSPACE
            }
            wrapper.addView(fromTv)
        }

        val bubble = TextView(this).apply {
            this.text = text
            textSize = 14f
            setTextColor(Color.WHITE)
            setPadding(16, 10, 16, 10)
            background = ContextCompat.getDrawable(
                this@MainActivity,
                if (isOutgoing) android.R.drawable.dialog_holo_dark_frame
                else android.R.drawable.dialog_holo_light_frame
            )
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also {
                it.gravity = if (isOutgoing) Gravity.END else Gravity.START
                it.maximumWidth = (resources.displayMetrics.widthPixels * 0.75).toInt()
            }
        }
        wrapper.addView(bubble)

        val tsTv = TextView(this).apply {
            this.text = ts
            textSize = 9f
            setTextColor(Color.GRAY)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.gravity = if (isOutgoing) Gravity.END else Gravity.START }
        }
        wrapper.addView(tsTv)
        chatContainer.addView(wrapper)
    }

    private fun addAnnounceCard(hash: String, name: String, ts: String) {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(16, 12, 16, 12)
            setBackgroundColor(Color.parseColor("#0f3460"))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.setMargins(0, 4, 0, 4) }
        }

        val nameTv = TextView(this).apply {
            text = if (name.isNotEmpty()) name else "Unknown node"
            textSize = 14f
            setTextColor(Color.WHITE)
        }
        card.addView(nameTv)

        val hashTv = TextView(this).apply {
            text = hash
            textSize = 10f
            setTextColor(Color.parseColor("#00d4ff"))
            typeface = android.graphics.Typeface.MONOSPACE
        }
        card.addView(hashTv)

        val tsTv = TextView(this).apply {
            text = "Seen at "
            textSize = 9f
            setTextColor(Color.GRAY)
        }
        card.addView(tsTv)

        // Tap to copy hash into dest field
        card.setOnClickListener {
            etDestHash.setText(hash.replace("<","").replace(">",""))
            showTab("chat")
            toast("Address copied to destination")
        }

        announcesContainer.addView(card)
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

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
            addLog("Requesting Bluetooth permissions...")
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
            addLog("Permissions granted!")
            setupBluetooth()
        } else {
            addLog("Permissions denied!")
        }
    }

    private fun setupBluetooth() {
        val bm = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        val ba = bm.adapter
        if (ba == null) { addLog("No Bluetooth!"); return }

        val paired = ba.bondedDevices?.toList() ?: emptyList()
        addLog("Found  paired device(s)")

        val names = paired.map { " ()" }
        spinnerDevices.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_item, names)
            .also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }

        btnConnect.setOnClickListener {
            val idx = spinnerDevices.selectedItemPosition
            if (idx < 0 || idx >= paired.size) return@setOnClickListener
            val device = paired[idx]
            addLog("Connecting to ...")
            btnConnect.isEnabled = false
            Thread {
                BluetoothService.connect(device) { socketWrapper, error ->
                    runOnUiThread {
                        if (error != null) {
                            addLog("BT error: System.Management.Automation.ParseException: At line:1 char:210
+ ... ineContextException: [StandaloneCoroutine{Cancelling}@95d7f52, Dispat ...
+                                                          ~~~~~~~~
Splatted variables like '@95d7f52' cannot be part of a comma-separated list of arguments.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) The term 'CoroutineScheduler.kt:684' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'CoroutineScheduler.kt:697' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'CoroutineScheduler.kt:793' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'CoroutineScheduler.kt:584' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'Tasks.kt:103' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'LimitedDispatcher.kt:115' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'DispatchedTask.kt:108' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'ContinuationImpl.kt:33' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'MainActivity.kt:66' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'RNSBridge.kt:7' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'Python.java:84' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'Native' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:104
+ ...                                                          at <python>. ...
+                                                                 ~
The '<' operator is reserved for future use.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:150
+ ...             com.chaquo.python.PyException: TypeError: module() takes  ...
+                                                                  ~
An expression was expected after '('.

At line:1 char:181
+ ... .PyException: TypeError: module() takes at most 2 arguments (3 given)
+                                                                    ~~~~~
Unexpected token 'given' in expression or statement.

At line:1 char:180
+ ... .PyException: TypeError: module() takes at most 2 arguments (3 given)
+                                                                   ~
Missing closing ')' in expression.

At line:1 char:186
+ ... .PyException: TypeError: module() takes at most 2 arguments (3 given)
+                                                                         ~
Unexpected token ')' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) The term 'Process:' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.735  4374-4374  AndroidRuntime          com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.735' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.474   820-820   audit                   auditd    ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.474' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.455   820-820   audit                   auditd    ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.455' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.454   820-820   audit                   auditd    ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.454' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.345  4374-4481  BluetoothSocket         com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.345' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:53.339  4374-4481  BluetoothSocket         com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:53.339' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.482  4374-4382  InputTransport          com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.482' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.481  4374-4394  OpenGLRenderer          com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.481' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.476  4374-4481  BluetoothSocket         com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.476' in expression or statement.

At line:1 char:109
+ ... cket         com.example.rnshello                 I  connect() for de ...
+                                                                  ~
An expression was expected after '('.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.469  4374-4481  BluetoothAdapter        com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.469' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.363  4374-4374  ViewRootIm...nActivity] com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.363' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:52.240  4374-4374  ViewRootIm...nActivity] com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:52.240' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:44.602  4374-4479  ProfileInstaller        com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:44.602' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:43.364  4374-4374  InputTransport          com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:43.364' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) System.Management.Automation.ParseException: At line:1 char:12
+ 2026-03-11 10:45:43.352  4374-4374  ViewRootIm...w:bee6a93] com.examp ...
+            ~~~~~~~~~~~~
Unexpected token '10:45:43.352' in expression or statement.
   at System.Management.Automation.Runspaces.PipelineBase.Invoke(IEnumerable input)
   at Microsoft.PowerShell.Executor.ExecuteCommandHelper(Pipeline tempPipeline, Exception& exceptionThrown, ExecutionOptions options) The term 'f:/' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. The term 'cd.' is not recognized as the name of a cmdlet, function, script file, or operable program. Check the spelling of the name, or if a path was included, verify that the path is correct and try again. Cannot find path 'F:\rns-hello-android' because it does not exist.")
                            btnConnect.isEnabled = true
                        } else {
                            addLog("BT connected. Starting RNS...")
                            Thread {
                                val addr = RNSBridge.start(socketWrapper!!)
                                runOnUiThread {
                                    if (addr.startsWith("Error")) {
                                        addLog("RNS error: ")
                                        btnConnect.isEnabled = true
                                    } else {
                                        tvMyAddress.text = "My address: "
                                        addLog("Announced: ")
                                        startPolling()
                                    }
                                }
                            }.start()
                        }
                    }
                }
            }.start()
        }
    }

    private fun addLog(msg: String) {
        // Just use Toast for connection status logs now
        runOnUiThread { toast(msg) }
    }

    override fun onDestroy() {
        super.onDestroy()
        refreshRunnable?.let { handler.removeCallbacks(it) }
    }
}
