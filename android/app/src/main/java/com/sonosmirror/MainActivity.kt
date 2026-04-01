package com.sonosmirror

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*

class MainActivity : AppCompatActivity() {

    private lateinit var statusText: TextView
    private lateinit var speakerListView: ListView
    private lateinit var refreshBtn: Button
    private lateinit var streamBtn: Button
    private lateinit var streamInfo: TextView

    private var speakers = listOf<SonosSpeaker>()
    private var selectedSpeaker: SonosSpeaker? = null
    private var streaming = false
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startStreaming(result.resultCode, result.data!!)
        } else {
            statusText.text = "Permission denied"
        }
    }

    private val notifPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> requestMediaProjection() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.statusText)
        speakerListView = findViewById(R.id.speakerList)
        refreshBtn = findViewById(R.id.refreshBtn)
        streamBtn = findViewById(R.id.streamBtn)
        streamInfo = findViewById(R.id.streamInfo)

        speakerListView.setOnItemClickListener { _, view, position, _ ->
            selectedSpeaker = speakers[position]
            streamBtn.isEnabled = true
            streamBtn.text = "Stream to ${speakers[position].name}"
            for (i in 0 until speakerListView.childCount) {
                speakerListView.getChildAt(i)?.setBackgroundColor(0xFF1E1E1E.toInt())
            }
            view.setBackgroundColor(0xFF2E7D32.toInt())
        }

        refreshBtn.setOnClickListener { discoverSpeakers() }
        streamBtn.setOnClickListener {
            if (streaming) stopStreaming() else beginStreaming()
        }

        discoverSpeakers()
    }

    private fun discoverSpeakers() {
        statusText.text = "Searching for Sonos speakers..."
        refreshBtn.isEnabled = false
        scope.launch {
            val found = withContext(Dispatchers.IO) { SonosController.discover(4000) }
            speakers = found
            refreshBtn.isEnabled = true
            statusText.text = if (found.isEmpty()) "No speakers found" else "Tap a speaker to select"
            speakerListView.adapter = ArrayAdapter(
                this@MainActivity, android.R.layout.simple_list_item_1,
                found.map { "${it.name}  (${it.ip})" }
            )
        }
    }

    private fun beginStreaming() {
        if (selectedSpeaker == null) { statusText.text = "Select a speaker first"; return }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS); return
        }
        requestMediaProjection()
    }

    private fun requestMediaProjection() {
        val pm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projectionLauncher.launch(pm.createScreenCaptureIntent())
    }

    private fun startStreaming(resultCode: Int, data: Intent) {
        val speaker = selectedSpeaker ?: return
        val intent = Intent(this, AudioCaptureService::class.java).apply {
            action = AudioCaptureService.ACTION_START
            putExtra(AudioCaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(AudioCaptureService.EXTRA_RESULT_DATA, data)
            putExtra(AudioCaptureService.EXTRA_SONOS_IP, speaker.ip)
            putExtra(AudioCaptureService.EXTRA_SONOS_NAME, speaker.name)
        }
        startForegroundService(intent)
        streaming = true
        streamBtn.text = "Stop Streaming"
        streamBtn.backgroundTintList = ContextCompat.getColorStateList(this, android.R.color.holo_red_dark)
        statusText.text = "Streaming audio to ${speaker.name}"

        scope.launch {
            delay(3000)
            streamInfo.text = "Audio URL: ${AudioCaptureService.streamServer?.localUrl ?: "..."}"
        }
    }

    private fun stopStreaming() {
        startService(Intent(this, AudioCaptureService::class.java).apply {
            action = AudioCaptureService.ACTION_STOP
        })
        selectedSpeaker?.let { sp ->
            scope.launch { try { SonosController.stop(sp) } catch (_: Exception) {} }
        }
        streaming = false
        streamBtn.text = "Stream to ${selectedSpeaker?.name ?: "Sonos"}"
        streamBtn.backgroundTintList = ContextCompat.getColorStateList(this, android.R.color.holo_green_dark)
        statusText.text = "Stopped"
        streamInfo.text = ""
    }

    override fun onDestroy() { scope.cancel(); super.onDestroy() }
}
