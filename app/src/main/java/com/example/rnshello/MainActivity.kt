package com.example.rnshello

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.chaquo.python.android.AndroidPlatform
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private lateinit var btService: BluetoothService
    private var rns: RNSBridge? = null
    private var pairedDevices: List<BluetoothDevice> = emptyList()

    private lateinit var spinner: Spinner
    private lateinit var tvAddress: TextView
    private lateinit var tvLog: TextView

    // Permission launcher - runs when user responds to permission dialog
    private val requestPermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        val allGranted = results.values.all { it }
        if (allGranted) {
            log("Permissions granted!")
            loadPairedDevices()
        } else {
            log("Bluetooth permissions denied - cannot continue")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        setContentView(R.layout.activity_main)
        btService = BluetoothService()

        spinner = findViewById(R.id.spinnerDevices)
        tvAddress = findViewById(R.id.tvAddress)
        tvLog = findViewById(R.id.tvLog)
        val btnConnect = findViewById<Button>(R.id.btnConnect)
        val etDest = findViewById<EditText>(R.id.etDestHash)
        val btnSend = findViewById<Button>(R.id.btnSend)

        // Check and request permissions first
        checkAndRequestPermissions()

        btnConnect.setOnClickListener {
            val mac = pairedDevices.getOrNull(spinner.selectedItemPosition)?.address
            if (mac == null) { log("No device selected"); return@setOnClickListener }
            lifecycleScope.launch {
                log("Connecting to $mac...")
                if (btService.connect(mac)) {
                    log("BT connected. Starting RNS...")
                    val addr = withContext(Dispatchers.IO) {
                        rns = RNSBridge(btService)
                        rns!!.start()
                    }
                    tvAddress.text = "LXMF: $addr"
                    log("Announced: $addr")
                } else {
                    log("BT connection failed")
                }
            }
        }

        btnSend.setOnClickListener {
            val dest = etDest.text.toString().trim()
            if (dest.isEmpty()) { log("Enter destination hash"); return@setOnClickListener }
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) { rns?.sendHello(dest) ?: "Not connected" }
                log("Send: $result")
            }
        }
    }

    private fun checkAndRequestPermissions() {
        val needed = mutableListOf<String>()

        // Android 12+ needs these runtime permissions
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.BLUETOOTH_CONNECT)
            }
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN)
                != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.BLUETOOTH_SCAN)
            }
        }

        if (needed.isNotEmpty()) {
            log("Requesting Bluetooth permissions...")
            requestPermissions.launch(needed.toTypedArray())
        } else {
            loadPairedDevices()
        }
    }

    private fun loadPairedDevices() {
        try {
            val adapter = BluetoothAdapter.getDefaultAdapter()
            if (adapter == null) { log("No Bluetooth adapter found"); return }
            if (!adapter.isEnabled) { log("Please enable Bluetooth first"); return }
            pairedDevices = adapter.bondedDevices.toList()
            if (pairedDevices.isEmpty()) {
                log("No paired devices found - pair your RNode in Android Bluetooth settings first")
            } else {
                log("Found ${pairedDevices.size} paired device(s)")
                spinner.adapter = ArrayAdapter(this,
                    android.R.layout.simple_spinner_item,
                    pairedDevices.map { "${it.name} — ${it.address}" })
            }
        } catch (e: SecurityException) {
            log("Permission error: ${e.message}")
        }
    }

    private fun log(msg: String) {
        tvLog.append("\n$msg")
    }
}
