package com.example.rnshello

import android.bluetooth.BluetoothAdapter
import android.os.Bundle
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.chaquo.python.android.AndroidPlatform
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private lateinit var btService: BluetoothService
    private var rns: RNSBridge? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        setContentView(R.layout.activity_main)
        btService = BluetoothService()

        val spinner = findViewById<Spinner>(R.id.spinnerDevices)
        val btnConnect = findViewById<Button>(R.id.btnConnect)
        val tvAddress = findViewById<TextView>(R.id.tvAddress)
        val etDest = findViewById<EditText>(R.id.etDestHash)
        val btnSend = findViewById<Button>(R.id.btnSend)
        val tvLog = findViewById<TextView>(R.id.tvLog)

        val paired = BluetoothAdapter.getDefaultAdapter().bondedDevices.toList()
        spinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_item,
            paired.map { " — " })

        btnConnect.setOnClickListener {
            val mac = paired.getOrNull(spinner.selectedItemPosition)?.address ?: return@setOnClickListener
            lifecycleScope.launch {
                tvLog.append("\nConnecting to $mac...")
                if (btService.connect(mac)) {
                    tvLog.append("\nBT connected. Starting RNS...")
                    val addr = withContext(Dispatchers.IO) {
                        rns = RNSBridge(btService)
                        rns!!.start()
                    }
                    tvAddress.text = "LXMF: $addr"
                    tvLog.append("\nAnnounced: $addr")
                } else {
                    tvLog.append("\nBT connection failed")
                }
            }
        }

        btnSend.setOnClickListener {
            val dest = etDest.text.toString().trim()
            if (dest.isEmpty()) { tvLog.append("\nEnter destination hash"); return@setOnClickListener }
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) { rns?.sendHello(dest) ?: "Not connected" }
                tvLog.append("\nSend: $result")
            }
        }
    }
}
