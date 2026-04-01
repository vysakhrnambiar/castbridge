package com.sonosmirror

import android.util.Log
import kotlinx.coroutines.*
import java.net.*
import java.io.*

private const val TAG = "SonosController"

data class SonosSpeaker(
    val name: String,
    val ip: String,
    val uuid: String
)

object SonosController {

    /**
     * Discover Sonos speakers via UPnP SSDP multicast
     */
    suspend fun discover(timeoutMs: Int = 3000): List<SonosSpeaker> = withContext(Dispatchers.IO) {
        val speakers = mutableMapOf<String, SonosSpeaker>()

        try {
            val ssdpAddr = InetAddress.getByName("239.255.255.250")
            val ssdpPort = 1900
            val searchMsg = """
                M-SEARCH * HTTP/1.1
                HOST: 239.255.255.250:1900
                MAN: "ssdp:discover"
                MX: 2
                ST: urn:schemas-upnp-org:device:ZonePlayer:1

            """.trimIndent().replace("\n", "\r\n")

            val socket = DatagramSocket(null)
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(0))
            socket.soTimeout = timeoutMs

            val sendData = searchMsg.toByteArray()
            val sendPacket = DatagramPacket(sendData, sendData.size, ssdpAddr, ssdpPort)
            socket.send(sendPacket)

            val buf = ByteArray(4096)
            val deadline = System.currentTimeMillis() + timeoutMs

            while (System.currentTimeMillis() < deadline) {
                try {
                    val recvPacket = DatagramPacket(buf, buf.size)
                    socket.receive(recvPacket)
                    val response = String(recvPacket.data, 0, recvPacket.length)
                    val ip = recvPacket.address.hostAddress ?: continue

                    // Extract UUID from USN header
                    val usnMatch = Regex("USN:\\s*uuid:(.+?)(?:::|\\s)").find(response)
                    val uuid = usnMatch?.groupValues?.get(1) ?: ip

                    if (!speakers.containsKey(ip)) {
                        // Fetch friendly name from device description
                        val name = fetchSpeakerName(ip) ?: "Sonos ($ip)"
                        speakers[ip] = SonosSpeaker(name, ip, uuid)
                    }
                } catch (e: SocketTimeoutException) {
                    break
                }
            }

            socket.close()
        } catch (e: Exception) {
            e.printStackTrace()
        }

        speakers.values.toList()
    }

    private fun fetchSpeakerName(ip: String): String? {
        return try {
            val url = URL("http://$ip:1400/xml/device_description.xml")
            val conn = url.openConnection() as HttpURLConnection
            conn.connectTimeout = 2000
            conn.readTimeout = 2000
            val xml = conn.inputStream.bufferedReader().readText()
            conn.disconnect()

            // Extract <roomName> from XML
            val match = Regex("<roomName>(.+?)</roomName>").find(xml)
            match?.groupValues?.get(1)
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Tell a Sonos speaker to play a stream URL
     */
    suspend fun playStream(speaker: SonosSpeaker, streamUrl: String, title: String = "Phone Audio") =
        withContext(Dispatchers.IO) {
            Log.i(TAG, "playStream: url=$streamUrl speaker=${speaker.ip}")
            // Use plain http:// for WAV streams
            val sonosUrl = streamUrl

            // Send with empty metadata — simpler and avoids XML escaping issues
            val soapBody = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID>
<CurrentURI>$sonosUrl</CurrentURI>
<CurrentURIMetaData></CurrentURIMetaData>
</u:SetAVTransportURI>
</s:Body>
</s:Envelope>"""

            Log.i(TAG, "Sending SetAVTransportURI to ${speaker.ip}")
            sendSoapAction(speaker.ip, "SetAVTransportURI", soapBody)
            Log.i(TAG, "SetAVTransportURI success, sending Play")

            // Now send Play
            val playBody = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID>
<Speed>1</Speed>
</u:Play>
</s:Body>
</s:Envelope>"""

            sendSoapAction(speaker.ip, "Play", playBody)
            Log.i(TAG, "Play command sent successfully")
        }

    /**
     * Stop playback
     */
    suspend fun stop(speaker: SonosSpeaker) = withContext(Dispatchers.IO) {
        val stopBody = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID>
</u:Stop>
</s:Body>
</s:Envelope>"""

        sendSoapAction(speaker.ip, "Stop", stopBody)
    }

    private fun sendSoapAction(ip: String, action: String, body: String) {
        val url = URL("http://$ip:1400/MediaRenderer/AVTransport/Control")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "text/xml; charset=utf-8")
        conn.setRequestProperty(
            "SOAPAction",
            "\"urn:schemas-upnp-org:service:AVTransport:1#$action\""
        )
        conn.doOutput = true
        conn.connectTimeout = 5000
        conn.readTimeout = 5000

        conn.outputStream.use { it.write(body.toByteArray()) }

        val responseCode = conn.responseCode
        if (responseCode != 200) {
            val error = conn.errorStream?.bufferedReader()?.readText() ?: "Unknown error"
            throw IOException("SOAP $action failed ($responseCode): $error")
        }
        conn.disconnect()
    }
}
