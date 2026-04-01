package com.sonosmirror

import android.util.Log
import java.io.*
import java.net.*
import java.util.concurrent.CopyOnWriteArrayList

class AudioStreamServer(private val port: Int = 8766) {

    companion object {
        private const val TAG = "AudioStreamServer"
    }

    private var serverSocket: ServerSocket? = null
    private var running = false
    private val clients = CopyOnWriteArrayList<OutputStream>()
    private var serverThread: Thread? = null

    val localUrl: String
        get() {
            val ip = getLocalIpAddress()
            return "http://$ip:$port/stream.mp3"
        }

    fun start() {
        if (running) return
        running = true
        Log.i(TAG, "Starting stream server on port $port")

        serverThread = Thread {
            try {
                serverSocket = ServerSocket(port)
                serverSocket?.soTimeout = 1000
                Log.i(TAG, "Server listening on $localUrl")

                while (running) {
                    try {
                        val clientSocket = serverSocket?.accept() ?: continue
                        Log.i(TAG, "Client connected: ${clientSocket.inetAddress}")
                        handleClient(clientSocket)
                    } catch (e: SocketTimeoutException) {
                        // Normal
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server error", e)
            }
        }.apply {
            isDaemon = true
            name = "AudioStreamServer"
            start()
        }
    }

    fun stop() {
        Log.i(TAG, "Stopping stream server")
        running = false
        clients.forEach { try { it.close() } catch (_: Exception) {} }
        clients.clear()
        try { serverSocket?.close() } catch (_: Exception) {}
        serverThread?.join(2000)
    }

    fun pushAudioData(data: ByteArray) {
        val deadClients = mutableListOf<OutputStream>()

        for (client in clients) {
            try {
                client.write(data)
                client.flush()
            } catch (e: Exception) {
                deadClients.add(client)
            }
        }

        if (deadClients.isNotEmpty()) {
            Log.w(TAG, "Removing ${deadClients.size} dead client(s)")
            clients.removeAll(deadClients.toSet())
        }
    }

    val clientCount: Int get() = clients.size

    private fun handleClient(socket: Socket) {
        Thread {
            try {
                val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
                var line = reader.readLine()
                while (line != null && line.isNotEmpty()) {
                    line = reader.readLine()
                }

                // Serve as audio/mpeg — Sonos accepts this content type
                val headers = "HTTP/1.1 200 OK\r\n" +
                    "Content-Type: audio/mpeg\r\n" +
                    "Connection: keep-alive\r\n" +
                    "Cache-Control: no-cache, no-store\r\n" +
                    "icy-name: Phone Audio Mirror\r\n" +
                    "\r\n"

                val out = socket.getOutputStream()
                out.write(headers.toByteArray())
                out.flush()

                clients.add(out)
                Log.i(TAG, "Client streaming. Total clients: ${clients.size}")

            } catch (e: Exception) {
                Log.e(TAG, "Client handler error", e)
                try { socket.close() } catch (_: Exception) {}
            }
        }.apply {
            isDaemon = true
            name = "StreamClient-${socket.inetAddress}"
            start()
        }
    }

    fun getLocalIpAddress(): String {
        try {
            // First pass: prefer wlan (WiFi) interfaces
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val name = iface.name.lowercase()
                if (name.startsWith("wlan") || name.startsWith("wifi") || name.startsWith("ap")) {
                    val addresses = iface.inetAddresses
                    while (addresses.hasMoreElements()) {
                        val addr = addresses.nextElement()
                        if (addr is Inet4Address && !addr.isLoopbackAddress) {
                            Log.i(TAG, "Using WiFi interface $name: ${addr.hostAddress}")
                            return addr.hostAddress ?: "0.0.0.0"
                        }
                    }
                }
            }
            // Second pass: any private network IP (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
            val interfaces2 = NetworkInterface.getNetworkInterfaces()
            while (interfaces2.hasMoreElements()) {
                val iface = interfaces2.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (addr is Inet4Address && !addr.isLoopbackAddress && addr.isSiteLocalAddress) {
                        val ip = addr.hostAddress ?: continue
                        Log.i(TAG, "Using local interface ${iface.name}: $ip")
                        return ip
                    }
                }
            }
            // Fallback
            val interfaces3 = NetworkInterface.getNetworkInterfaces()
            while (interfaces3.hasMoreElements()) {
                val iface = interfaces3.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        Log.w(TAG, "Fallback interface ${iface.name}: ${addr.hostAddress}")
                        return addr.hostAddress ?: "0.0.0.0"
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to get IP", e)
        }
        return "0.0.0.0"
    }
}
