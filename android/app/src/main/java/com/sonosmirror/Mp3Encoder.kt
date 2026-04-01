package com.sonosmirror

import android.util.Log
import com.github.axet.lamejni.Lame

private const val TAG = "Mp3Encoder"

class Mp3LameEncoder(
    private val sampleRate: Int = 44100,
    private val channelCount: Int = 1,
    private val bitRate: Int = 128  // kbps
) {
    private var lame: Lame? = null

    fun start() {
        lame = Lame()
        // open(channels, sampleRate, bitrateKbps, vbrQuality)
        lame?.open(channelCount, sampleRate, bitRate, 2)
        Log.i(TAG, "LAME opened: ch=$channelCount rate=$sampleRate br=${bitRate}kbps q=2")
    }

    fun stop() {
        try { lame?.close() } catch (_: Exception) {}
        lame = null
    }

    fun encode(pcmData: ByteArray): ByteArray {
        val l = lame ?: return ByteArray(0)

        // Convert bytes to short array (16-bit little-endian PCM)
        val numShorts = pcmData.size / 2
        val shorts = ShortArray(numShorts)
        for (i in 0 until numShorts) {
            shorts[i] = ((pcmData[i * 2].toInt() and 0xFF) or
                    (pcmData[i * 2 + 1].toInt() shl 8)).toShort()
        }

        // encode(buffer, pos=0, len=numShorts)
        return try {
            val result = l.encode(shorts, 0, numShorts)
            result ?: ByteArray(0)
        } catch (e: Exception) {
            Log.e(TAG, "encode error: ${e.message}")
            ByteArray(0)
        }
    }

    fun flush(): ByteArray {
        val l = lame ?: return ByteArray(0)
        return try {
            // encode(null, 0, 0) = flush
            l.encode(null, 0, 0) ?: ByteArray(0)
        } catch (_: Exception) {
            ByteArray(0)
        }
    }
}
