package com.sonosmirror

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.*
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.*
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*

class AudioCaptureService : Service() {

    companion object {
        private const val TAG = "AudioCaptureService"
        const val CHANNEL_ID = "audio_mirror_channel"
        const val NOTIFICATION_ID = 1
        const val ACTION_START = "START"
        const val ACTION_STOP = "STOP"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_RESULT_DATA = "resultData"
        const val EXTRA_SONOS_IP = "sonosIp"
        const val EXTRA_SONOS_NAME = "sonosName"

        var isRunning = false
            private set

        var streamServer: AudioStreamServer? = null
            private set

        var statusMessage: String = ""
            private set
    }

    private var mediaProjection: MediaProjection? = null
    private var audioRecord: AudioRecord? = null
    private var encoder: Mp3LameEncoder? = null
    private var captureJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
                @Suppress("DEPRECATION")
                val resultData = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
                val sonosIp = intent.getStringExtra(EXTRA_SONOS_IP) ?: ""
                val sonosName = intent.getStringExtra(EXTRA_SONOS_NAME) ?: "Sonos"

                Log.i(TAG, "Starting audio capture. Sonos: $sonosName @ $sonosIp")
                startForegroundWithNotification(sonosName)
                startCapture(resultCode, resultData, sonosIp, sonosName)
            }
            ACTION_STOP -> {
                Log.i(TAG, "Stopping capture")
                stopCapture()
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun startForegroundWithNotification(speakerName: String) {
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Sonos Audio Mirror")
            .setContentText("Streaming audio to $speakerName")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun startCapture(resultCode: Int, resultData: Intent?, sonosIp: String, sonosName: String) {
        if (resultData == null) {
            Log.e(TAG, "resultData is null!")
            return
        }

        try {
            val projectionManager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mediaProjection = projectionManager.getMediaProjection(resultCode, resultData)

            mediaProjection?.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    Log.w(TAG, "MediaProjection stopped by system")
                    stopCapture()
                }
            }, Handler(Looper.getMainLooper()))

            // Start HTTP stream server
            val server = AudioStreamServer(8766)
            server.start()
            streamServer = server
            Log.i(TAG, "Stream server started at ${server.localUrl}")

            // Start LAME MP3 encoder
            val enc = Mp3LameEncoder(sampleRate = 44100, channelCount = 1, bitRate = 128)
            enc.start()
            encoder = enc

            // Audio capture config
            val audioFormat = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(44100)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build()

            val config = AudioPlaybackCaptureConfiguration.Builder(mediaProjection!!)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                .build()

            val bufferSize = AudioRecord.getMinBufferSize(
                44100, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )

            audioRecord = AudioRecord.Builder()
                .setAudioPlaybackCaptureConfig(config)
                .setAudioFormat(audioFormat)
                .setBufferSizeInBytes(maxOf(bufferSize * 4, 16384))
                .build()

            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord failed to initialize!")
                statusMessage = "Audio capture failed"
                return
            }

            audioRecord?.startRecording()
            isRunning = true

            // Tell Sonos to play our audio stream
            scope.launch {
                try {
                    delay(2000)
                    val speaker = SonosSpeaker(sonosName, sonosIp, "")
                    SonosController.playStream(speaker, server.localUrl, "Phone Audio")
                    statusMessage = "Audio -> $sonosName"
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to tell Sonos to play", e)
                    statusMessage = "Sonos error: ${e.message}"
                }
            }

            // Capture loop
            captureJob = scope.launch {
                val buffer = ByteArray(4096)
                var totalPcm = 0L
                var totalMp3 = 0L

                while (isActive && isRunning) {
                    val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                    if (read > 0) {
                        totalPcm += read
                        val mp3Data = encoder?.encode(buffer.copyOf(read))
                        if (mp3Data != null && mp3Data.isNotEmpty()) {
                            totalMp3 += mp3Data.size
                            server.pushAudioData(mp3Data)
                        }

                        if (totalPcm % (44100 * 2 * 5) < 4096) {
                            Log.d(TAG, "PCM:${totalPcm/1024}KB MP3:${totalMp3/1024}KB Clients:${server.clientCount}")
                        }
                    }
                }
                Log.i(TAG, "Done. PCM:${totalPcm/1024}KB MP3:${totalMp3/1024}KB")
            }

        } catch (e: Exception) {
            Log.e(TAG, "startCapture failed", e)
            statusMessage = "Error: ${e.message}"
        }
    }

    private fun stopCapture() {
        isRunning = false
        captureJob?.cancel()

        try { audioRecord?.stop() } catch (_: Exception) {}
        try { audioRecord?.release() } catch (_: Exception) {}
        audioRecord = null

        encoder?.stop()
        encoder = null

        streamServer?.stop()
        streamServer = null

        mediaProjection?.stop()
        mediaProjection = null

        statusMessage = "Stopped"
    }

    override fun onDestroy() {
        stopCapture()
        scope.cancel()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "Audio Streaming", NotificationManager.IMPORTANCE_LOW
        ).apply { description = "Shows when audio is being streamed to Sonos" }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }
}
