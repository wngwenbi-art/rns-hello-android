Set-Content app/src/main/kotlin/com/reticulum/mesh/MainActivity.kt @'
package com.reticulum.mesh

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.view.ViewGroup
import android.widget.*
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.ByteArrayOutputStream
import java.io.File

// This class receives callbacks from Python!
class PythonMessageListener(private val activity: MainActivity) {
    fun onMessageReceived(sender: String, text: String, imgPath: String) {
        activity.runOnUiThread {
            activity.displayMessage(sender, text, imgPath, false)
        }
    }
}

class MainActivity : Activity() {
    private lateinit var deviceSpinner: Spinner
    private lateinit var startBtn: Button
    private lateinit var sendBtn: Button
    private lateinit var attachBtn: Button
    private lateinit var announceBtn: Button
    private lateinit var msgInput: EditText
    private lateinit var destInput: EditText
    private lateinit var statusText: TextView
    private lateinit var chatScroll: ScrollView
    private lateinit var chatLayout: LinearLayout
    
    private var selectedImageBytes: ByteArray? = null
    private var pairedDevices = listOf<BluetoothDevice>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        val mainLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(20, 20, 20, 20)
        }

        statusText = TextView(this).apply { text = "Offline"; textSize = 14f; setPadding(0,0,0,10) }
        deviceSpinner = Spinner(this)
        startBtn = Button(this).apply { text = "Connect RNode" }
        
        // --- Chat History Area ---
        chatScroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1.0f)
            setPadding(0, 20, 0, 20)
        }
        chatLayout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        chatScroll.addView(chatLayout)

        // --- Controls Area ---
        destInput = EditText(this).apply { hint = "Recipient Hash"; visibility = android.view.View.GONE }
        msgInput = EditText(this).apply { hint = "Message"; visibility = android.view.View.GONE }
        
        val buttonRow1 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        attachBtn = Button(this).apply { text = "Img"; visibility = android.view.View.GONE }
        sendBtn = Button(this).apply { text = "Send"; visibility = android.view.View.GONE }
        announceBtn = Button(this).apply { text = "Announce"; visibility = android.view.View.GONE }
        
        buttonRow1.addView(attachBtn)
        buttonRow1.addView(sendBtn)
        buttonRow1.addView(announceBtn)

        startBtn.setOnClickListener {
            val selectedPos = deviceSpinner.selectedItemPosition
            if (selectedPos in pairedDevices.indices) startMesh(pairedDevices[selectedPos])
        }

        attachBtn.setOnClickListener {
            val intent = Intent(Intent.ACTION_PICK, MediaStore.Images.Media.EXTERNAL_CONTENT_URI)
            startActivityForResult(intent, 102)
        }

        announceBtn.setOnClickListener {
            val py = Python.getInstance()
            py.getModule("reticulum_wrapper").callAttr("get_instance").callAttr("announce_now")
            Toast.makeText(this, "Announce broadcasted!", Toast.LENGTH_SHORT).show()
        }

        sendBtn.setOnClickListener {
            val dest = destInput.text.toString()
            val msg = msgInput.text.toString()
            if (dest.length >= 10) {
                sendMessage(dest, msg)
                // Add our sent message to the chat UI
                val imagePath = saveSentImageLocally()
                displayMessage("Me", msg, imagePath, true)
                msgInput.setText("")
                selectedImageBytes = null
                attachBtn.text = "Img"
            } else {
                Toast.makeText(this, "Enter valid Hash", Toast.LENGTH_SHORT).show()
            }
        }

        mainLayout.addView(statusText)
        mainLayout.addView(deviceSpinner)
        mainLayout.addView(startBtn)
        mainLayout.addView(chatScroll)
        mainLayout.addView(destInput)
        mainLayout.addView(msgInput)
        mainLayout.addView(buttonRow1)
        
        setContentView(mainLayout)
        checkPermissionsAndLoadDevices()
    }

    // Displays messages in the ScrollView
    fun displayMessage(sender: String, text: String, imgPath: String, isMe: Boolean) {
        val msgContainer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(15, 15, 15, 15)
            setBackgroundColor(if (isMe) Color.parseColor("#E1FFC7") else Color.parseColor("#FFFFFF"))
            
            val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            params.setMargins(if (isMe) 100 else 0, 10, if (isMe) 0 else 100, 10)
            layoutParams = params
        }
        
        val senderView = TextView(this).apply {
            this.text = if (isMe) "Me" else "Peer: " + sender.take(8)
            textSize = 10f
            setTextColor(Color.GRAY)
        }
        msgContainer.addView(senderView)
        
        if (imgPath.isNotEmpty() && File(imgPath).exists()) {
            val bmp = BitmapFactory.decodeFile(imgPath)
            if (bmp != null) {
                val imgView = ImageView(this).apply {
                    setImageBitmap(bmp)
                    adjustViewBounds = true
                    setPadding(0, 10, 0, 10)
                }
                msgContainer.addView(imgView)
            }
        }
        
        if (text.isNotEmpty()) {
            val textView = TextView(this).apply {
                this.text = text
                textSize = 16f
                setTextColor(Color.BLACK)
            }
            msgContainer.addView(textView)
        }
        
        chatLayout.addView(msgContainer)
        chatScroll.post { chatScroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun saveSentImageLocally(): String {
        val bytes = selectedImageBytes ?: return ""
        val dir = File(filesDir, "images")
        dir.mkdirs()
        val file = File(dir, "sent_" + System.currentTimeMillis() + ".webp")
        file.writeBytes(bytes)
        return file.absolutePath
    }

    private fun startMesh(device: BluetoothDevice) {
        startBtn.text = "Starting..."
        startBtn.isEnabled = false
        Thread {
            try {
                val bridge = KotlinRNodeBridge(device)
                if (bridge.connect()) {
                    val py = Python.getInstance()
                    val wrapper = py.getModule("reticulum_wrapper")
                    val instance = wrapper.callAttr("get_instance", filesDir.absolutePath)
                    
                    instance.callAttr("set_bridge", bridge)
                    
                    // Passing the Kotlin callback listener to Python!
                    instance.callAttr("set_callback", PythonMessageListener(this))
                    
                    val myHash = instance.callAttr("start_lxmf", "Android Node").toString()
                    
                    runOnUiThread {
                        statusText.text = "Hash: " + myHash
                        startBtn.visibility = android.view.View.GONE
                        deviceSpinner.visibility = android.view.View.GONE
                        destInput.visibility = android.view.View.VISIBLE
                        msgInput.visibility = android.view.View.VISIBLE
                        sendBtn.visibility = android.view.View.VISIBLE
                        attachBtn.visibility = android.view.View.VISIBLE
                        announceBtn.visibility = android.view.View.VISIBLE
                    }
                } else {
                    runOnUiThread {
                        Toast.makeText(this, "BT Connection Failed", Toast.LENGTH_LONG).show()
                        startBtn.isEnabled = true
                        startBtn.text = "Connect RNode"
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { 
                    Toast.makeText(this, "Error: " + e.message, Toast.LENGTH_LONG).show()
                    startBtn.isEnabled = true
                    startBtn.text = "Retry"
                }
            }
        }.start()
    }

    private fun sendMessage(dest: String, msg: String) {
        Thread {
            val py = Python.getInstance()
            py.getModule("reticulum_wrapper").callAttr("get_instance")
              .callAttr("send_message", dest, msg, selectedImageBytes)
        }.start()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 102 && resultCode == RESULT_OK && data != null) {
            val uri = data.data ?: return
            val inputStream = contentResolver.openInputStream(uri)
            val original = BitmapFactory.decodeStream(inputStream)
            
            val scaled = Bitmap.createScaledBitmap(original, 300, 300, true)
            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.WEBP, 30, out)
            selectedImageBytes = out.toByteArray()
            attachBtn.text = "Img (Attached)"
        }
    }

    private fun checkPermissionsAndLoadDevices() {
        val permissions = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.BLUETOOTH_SCAN)
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        if (permissions.any { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(permissions, 101)
        } else { loadPairedDevices() }
    }

    private fun loadPairedDevices() {
        val btManager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = btManager.adapter
        if (adapter != null && adapter.isEnabled) {
            pairedDevices = adapter.bondedDevices.toList()
            val names = pairedDevices.map { it.name ?: "Unknown" }
            deviceSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, names)
        }
    }
}
'@