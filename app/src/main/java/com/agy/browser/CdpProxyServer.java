package com.agy.browser;

import android.content.Context;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.os.Process;
import android.text.TextUtils;
import android.util.Base64;
import android.util.Log;

import com.agy.util.AtomicFileWriter;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Localhost-only Chrome DevTools Protocol (CDP) proxy bridging {@code 127.0.0.1:<port>}
 * to Android WebView's {@code @webview_devtools_remote_<pid>} abstract Unix socket.
 *
 * <p>Writes {@code DevToolsActivePort} into Chromium config directories under {@code $HOME}
 * so Antigravity detects active browser debugging capabilities, while strictly isolating
 * exposed CDP targets to {@link AgentBrowserManager}-owned WebViews (never the main UI WebView).
 */
public final class CdpProxyServer {

    private static final String TAG = "CdpProxyServer";
    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int IO_TIMEOUT_MS = 10000;

    private final Context appContext;
    private final AgentBrowserManager manager;
    private final Set<Socket> activeSockets = Collections.newSetFromMap(new ConcurrentHashMap<>());
    private ExecutorService clientExecutor;
    private Thread acceptThread;
    private volatile ServerSocket serverSocket;
    private volatile boolean running = false;
    private volatile int port = 0;

    public CdpProxyServer(Context context, AgentBrowserManager manager) {
        this.appContext = context.getApplicationContext();
        this.manager = manager;
    }

    public int getPort() {
        return port;
    }

    public boolean isRunning() {
        return running;
    }

    public synchronized void start() {
        if (running) {
            return;
        }
        manager.ensureDebuggingEnabled();
        try {
            serverSocket = new ServerSocket(0, 32, InetAddress.getByName("127.0.0.1"));
            port = serverSocket.getLocalPort();
            manager.setCdpProxyPort(port);
            running = true;

            writeDevToolsActivePortFiles(port);

            clientExecutor = Executors.newCachedThreadPool(r -> {
                Thread t = new Thread(r, "agy-cdp-worker");
                t.setDaemon(true);
                return t;
            });

            acceptThread = new Thread(this::acceptLoop, "agy-cdp-accept");
            acceptThread.setDaemon(true);
            acceptThread.start();
            Log.i(TAG, "CDP proxy started on 127.0.0.1:" + port);
        } catch (IOException e) {
            Log.e(TAG, "Failed to start CDP proxy server", e);
            stop();
        }
    }

    public synchronized void stop() {
        running = false;
        port = 0;
        manager.setCdpProxyPort(0);
        removeDevToolsActivePortFiles();

        if (serverSocket != null) {
            try {
                serverSocket.close();
            } catch (IOException ignored) {
            }
            serverSocket = null;
        }
        for (Socket s : activeSockets) {
            closeQuietly(s);
        }
        activeSockets.clear();
        if (clientExecutor != null) {
            clientExecutor.shutdownNow();
            clientExecutor = null;
        }
        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }
    }

    /**
     * Queries {@code /json/list} directly from Android WebView's abstract DevTools socket.
     */
    public static JSONArray fetchRawTargets() throws IOException {
        RawHttpResponse resp = fetchFromRawDevTools("/json/list");
        if (resp.statusCode != 200) {
            throw new IOException("Raw /json/list returned HTTP " + resp.statusCode);
        }
        String body = new String(resp.body, StandardCharsets.UTF_8).trim();
        if (body.isEmpty()) {
            return new JSONArray();
        }
        try {
            return new JSONArray(body);
        } catch (Exception e) {
            throw new IOException("Invalid JSON from raw /json/list", e);
        }
    }

    public static LocalSocket openRawDevToolsSocket() throws IOException {
        LocalSocket ls = new LocalSocket();
        String socketName = "webview_devtools_remote_" + Process.myPid();
        ls.connect(new LocalSocketAddress(socketName, LocalSocketAddress.Namespace.ABSTRACT));
        return ls;
    }

    private File[] devToolsActivePortFiles() {
        File filesDir = appContext.getFilesDir();
        return new File[]{
                new File(filesDir, ".config/google-chrome/DevToolsActivePort"),
                new File(filesDir, ".config/chromium/DevToolsActivePort")
        };
    }

    private void writeDevToolsActivePortFiles(int listenPort) {
        String content = listenPort + "\n/devtools/browser\n";
        for (File f : devToolsActivePortFiles()) {
            try {
                File parent = f.getParentFile();
                if (parent != null && !parent.exists()) {
                    //noinspection ResultOfMethodCallIgnored
                    parent.mkdirs();
                }
                AtomicFileWriter.writeText(f, content);
            } catch (Exception e) {
                Log.w(TAG, "Failed writing DevToolsActivePort to " + f.getAbsolutePath(), e);
            }
        }
    }

    private void removeDevToolsActivePortFiles() {
        for (File f : devToolsActivePortFiles()) {
            try {
                if (f.exists()) {
                    //noinspection ResultOfMethodCallIgnored
                    f.delete();
                }
            } catch (Exception e) {
                Log.w(TAG, "Failed removing " + f.getAbsolutePath(), e);
            }
        }
    }

    private void acceptLoop() {
        while (running && serverSocket != null) {
            try {
                Socket client = serverSocket.accept();
                client.setTcpNoDelay(true);
                activeSockets.add(client);
                ExecutorService exec = clientExecutor;
                if (exec != null && !exec.isShutdown()) {
                    exec.execute(() -> handleClient(client));
                } else {
                    closeQuietly(client);
                }
            } catch (IOException e) {
                if (running) {
                    Log.w(TAG, "Accept error on CDP proxy", e);
                }
            }
        }
    }

    private void handleClient(Socket client) {
        try {
            client.setSoTimeout(IO_TIMEOUT_MS);
            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();

            HttpRequest req = readHttpRequest(in);
            if (req == null) {
                return;
            }

            // Clear SO_TIMEOUT once upgraded or during long-lived WS handling
            String upgrade = req.header("upgrade");
            boolean isWs = "websocket".equalsIgnoreCase(upgrade)
                    && !TextUtils.isEmpty(req.header("sec-websocket-key"));

            if (isWs) {
                client.setSoTimeout(0);
                handleWebSocket(client, in, out, req);
            } else {
                handleHttp(out, req);
            }
        } catch (Exception e) {
            if (running) {
                Log.d(TAG, "CDP client closed: " + e.getMessage());
            }
        } finally {
            activeSockets.remove(client);
            closeQuietly(client);
        }
    }

    private void handleHttp(OutputStream out, HttpRequest req) throws IOException {
        String path = req.path;

        if ("/json/version".equals(path)) {
            JSONObject version = buildVersionJson();
            sendJsonResponse(out, 200, "OK", version.toString());
            return;
        }

        if ("/json".equals(path) || "/json/list".equals(path)) {
            JSONArray list = buildFilteredTargetsJson();
            sendJsonResponse(out, 200, "OK", list.toString());
            return;
        }

        if ("/json/new".equals(path)) {
            String targetUrl = extractNewTabUrl(req.rawQuery);
            String targetId = manager.createTab(targetUrl);
            if (TextUtils.isEmpty(targetId)) {
                sendTextResponse(out, 500, "Internal Server Error", "Failed to create agent tab");
                return;
            }
            JSONObject targetObj = findFilteredTargetById(targetId);
            if (targetObj == null) {
                targetObj = new JSONObject();
                try {
                    targetObj.put("id", targetId);
                    targetObj.put("type", "page");
                    targetObj.put("title", "");
                    targetObj.put("url", TextUtils.isEmpty(targetUrl) ? "about:blank" : targetUrl);
                    targetObj.put("webSocketDebuggerUrl",
                            "ws://127.0.0.1:" + port + "/devtools/page/" + targetId);
                } catch (Exception ignored) {
                }
            }
            sendJsonResponse(out, 200, "OK", targetObj.toString());
            return;
        }

        if (path.startsWith("/json/close")) {
            String targetId = extractTargetIdSuffix(path, "/json/close", req.rawQuery);
            if (!manager.getRegisteredTargetIds().contains(targetId)) {
                sendTextResponse(out, 404, "Not Found", "No such agent target");
                return;
            }
            boolean closed = manager.closeTab(targetId);
            if (closed) {
                sendTextResponse(out, 200, "OK", "Target is closing");
            } else {
                sendTextResponse(out, 404, "Not Found", "Could not close target");
            }
            return;
        }

        if (path.startsWith("/json/activate")) {
            String targetId = extractTargetIdSuffix(path, "/json/activate", req.rawQuery);
            if (!manager.getRegisteredTargetIds().contains(targetId)) {
                sendTextResponse(out, 404, "Not Found", "No such agent target");
                return;
            }
            boolean activated = manager.activateTab(targetId);
            if (activated) {
                sendTextResponse(out, 200, "OK", "Target activated");
            } else {
                sendTextResponse(out, 404, "Not Found", "Could not activate target");
            }
            return;
        }

        if ("/json/protocol".equals(path)) {
            try {
                RawHttpResponse raw = fetchFromRawDevTools("/json/protocol");
                sendJsonResponse(out, raw.statusCode, "OK",
                        new String(raw.body, StandardCharsets.UTF_8));
            } catch (IOException e) {
                sendJsonResponse(out, 200, "OK", "{}");
            }
            return;
        }

        sendTextResponse(out, 404, "Not Found", "Not Found");
    }

    private void handleWebSocket(Socket client, InputStream clientIn, OutputStream clientOut, HttpRequest req)
            throws IOException {
        String path = req.path;

        if ("/devtools/browser".equals(path) || path.startsWith("/devtools/browser/")) {
            serveBrowserWebSocket(clientIn, clientOut, req);
            return;
        }

        if (path.startsWith("/devtools/page/")) {
            String targetId = path.substring("/devtools/page/".length());
            int slash = targetId.indexOf('/');
            if (slash >= 0) {
                targetId = targetId.substring(0, slash);
            }
            if (!manager.getRegisteredTargetIds().contains(targetId)) {
                sendTextResponse(clientOut, 404, "Not Found", "Target not registered as agent tab");
                return;
            }
            JSONObject matched = findFilteredTargetById(targetId);
            if (matched != null && manager.isMainUiUrl(matched.optString("url", ""))) {
                sendTextResponse(clientOut, 403, "Forbidden", "Access to main UI WebView denied");
                return;
            }
            tunnelPageWebSocket(client, clientIn, clientOut, req, targetId);
            return;
        }

        sendTextResponse(clientOut, 404, "Not Found", "Unknown WebSocket endpoint");
    }

    private void tunnelPageWebSocket(Socket client, InputStream clientIn, OutputStream clientOut,
                                     HttpRequest req, String targetId) throws IOException {
        LocalSocket rawSocket = null;
        try {
            rawSocket = openRawDevToolsSocket();
            InputStream rawIn = rawSocket.getInputStream();
            OutputStream rawOut = rawSocket.getOutputStream();

            StringBuilder upgrade = new StringBuilder();
            upgrade.append("GET /devtools/page/").append(targetId).append(" HTTP/1.1\r\n");
            upgrade.append("Host: localhost\r\n");
            upgrade.append("Upgrade: websocket\r\n");
            upgrade.append("Connection: Upgrade\r\n");
            upgrade.append("Sec-WebSocket-Key: ").append(req.header("sec-websocket-key")).append("\r\n");
            String ver = req.header("sec-websocket-version");
            upgrade.append("Sec-WebSocket-Version: ").append(!TextUtils.isEmpty(ver) ? ver : "13").append("\r\n");
            String proto = req.header("sec-websocket-protocol");
            if (!TextUtils.isEmpty(proto)) {
                upgrade.append("Sec-WebSocket-Protocol: ").append(proto).append("\r\n");
            }
            String ext = req.header("sec-websocket-extensions");
            if (!TextUtils.isEmpty(ext)) {
                upgrade.append("Sec-WebSocket-Extensions: ").append(ext).append("\r\n");
            }
            upgrade.append("\r\n");
            rawOut.write(upgrade.toString().getBytes(StandardCharsets.US_ASCII));
            rawOut.flush();

            // Forward raw 101 handshake response + any trailing initial WebSocket bytes
            byte[] headerAndTail = readUntilDoubleCrlf(rawIn);
            if (headerAndTail.length == 0) {
                sendTextResponse(clientOut, 502, "Bad Gateway", "Empty DevTools handshake");
                return;
            }
            String headerPreview = new String(headerAndTail, 0,
                    Math.min(headerAndTail.length, 64), StandardCharsets.US_ASCII);
            if (!headerPreview.contains(" 101 ")) {
                clientOut.write(headerAndTail);
                clientOut.flush();
                return;
            }
            clientOut.write(headerAndTail);
            clientOut.flush();

            final LocalSocket lsRef = rawSocket;
            ExecutorService exec = clientExecutor;
            if (exec == null || exec.isShutdown()) {
                return;
            }

            Thread downstream = new Thread(() -> {
                try {
                    pumpStream(rawIn, clientOut);
                } catch (IOException ignored) {
                } finally {
                    closeQuietly(client);
                    closeQuietly(lsRef);
                }
            }, "agy-cdp-ws-down");
            downstream.setDaemon(true);
            downstream.start();

            try {
                pumpStream(clientIn, rawOut);
            } finally {
                closeQuietly(rawSocket);
                closeQuietly(client);
            }
        } finally {
            closeQuietly(rawSocket);
        }
    }

    private void serveBrowserWebSocket(InputStream clientIn, OutputStream clientOut, HttpRequest req)
            throws IOException {
        String key = req.header("sec-websocket-key");
        String accept = computeWsAccept(key);
        String handshake = "HTTP/1.1 101 Switching Protocols\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Accept: " + accept + "\r\n\r\n";
        clientOut.write(handshake.getBytes(StandardCharsets.US_ASCII));
        clientOut.flush();

        final Object writeLock = new Object();
        while (running) {
            WsFrame frame = readWsFrame(clientIn);
            if (frame == null || frame.opcode == 0x8) {
                writeWsFrame(clientOut, writeLock, 0x8, new byte[0]);
                break;
            }
            if (frame.opcode == 0x9) {
                writeWsFrame(clientOut, writeLock, 0xA, frame.payload);
                continue;
            }
            if (frame.opcode != 0x1) {
                continue;
            }

            String text = new String(frame.payload, StandardCharsets.UTF_8);
            String reply = handleBrowserCdpRpc(text, clientOut, writeLock);
            if (reply != null) {
                writeWsFrame(clientOut, writeLock, 0x1, reply.getBytes(StandardCharsets.UTF_8));
            }
        }
    }

    private String handleBrowserCdpRpc(String rawJson, OutputStream clientOut, Object writeLock) {
        long id = -1;
        boolean hasId = false;
        try {
            JSONObject msg = new JSONObject(rawJson);
            if (msg.has("id")) {
                hasId = true;
                id = msg.getLong("id");
            }
            String method = msg.optString("method", "");
            JSONObject params = msg.optJSONObject("params");
            if (params == null) {
                params = new JSONObject();
            }

            switch (method) {
                case "Browser.getVersion": {
                    JSONObject ver = buildVersionJson();
                    JSONObject res = new JSONObject();
                    res.put("protocolVersion", ver.optString("Protocol-Version", "1.3"));
                    res.put("product", ver.optString("Browser", "Android WebView"));
                    res.put("revision", ver.optString("V8-Version", ""));
                    res.put("userAgent", ver.optString("User-Agent", ""));
                    res.put("jsVersion", ver.optString("V8-Version", ""));
                    return rpcResult(id, res);
                }
                case "Browser.close":
                    return rpcResult(id, new JSONObject());
                case "Target.getTargets": {
                    JSONArray infos = buildTargetInfosArray();
                    JSONObject res = new JSONObject();
                    res.put("targetInfos", infos);
                    return rpcResult(id, res);
                }
                case "Target.createTarget": {
                    String url = params.optString("url", "about:blank");
                    String targetId = manager.createTab(url);
                    if (TextUtils.isEmpty(targetId)) {
                        return rpcError(id, -32000, "Failed to create agent WebView target");
                    }
                    JSONObject res = new JSONObject();
                    res.put("targetId", targetId);
                    return rpcResult(id, res);
                }
                case "Target.closeTarget": {
                    String targetId = params.optString("targetId", "");
                    if (!manager.getRegisteredTargetIds().contains(targetId)) {
                        return rpcError(id, -32000, "No such agent target");
                    }
                    boolean ok = manager.closeTab(targetId);
                    JSONObject res = new JSONObject();
                    res.put("success", ok);
                    return rpcResult(id, res);
                }
                case "Target.activateTarget": {
                    String targetId = params.optString("targetId", "");
                    if (!manager.getRegisteredTargetIds().contains(targetId)
                            || !manager.activateTab(targetId)) {
                        return rpcError(id, -32000, "No such agent target");
                    }
                    return rpcResult(id, new JSONObject());
                }
                case "Target.getTargetInfo": {
                    String targetId = params.optString("targetId", manager.getSelectedTargetId());
                    JSONObject target = findFilteredTargetById(targetId);
                    if (target == null) {
                        return rpcError(id, -32000, "No such agent target");
                    }
                    JSONObject res = new JSONObject();
                    res.put("targetInfo", toTargetInfo(target));
                    return rpcResult(id, res);
                }
                case "Target.setDiscoverTargets": {
                    if (params.optBoolean("discover", false)) {
                        JSONArray infos = buildTargetInfosArray();
                        for (int i = 0; i < infos.length(); i++) {
                            JSONObject evt = new JSONObject();
                            evt.put("method", "Target.targetCreated");
                            JSONObject p = new JSONObject();
                            p.put("targetInfo", infos.getJSONObject(i));
                            evt.put("params", p);
                            writeWsFrame(clientOut, writeLock, 0x1,
                                    evt.toString().getBytes(StandardCharsets.UTF_8));
                        }
                    }
                    return rpcResult(id, new JSONObject());
                }
                default:
                    if (hasId) {
                        return rpcError(id, -32601, "Method not available on /devtools/browser proxy: " + method);
                    }
                    return null;
            }
        } catch (Exception e) {
            return hasId ? rpcError(id, -32603, "Internal error: " + e.getMessage()) : null;
        }
    }

    private JSONObject buildVersionJson() {
        JSONObject out = new JSONObject();
        try {
            RawHttpResponse raw = fetchFromRawDevTools("/json/version");
            if (raw.statusCode == 200) {
                out = new JSONObject(new String(raw.body, StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
        try {
            if (!out.has("Browser")) {
                out.put("Browser", "Android WebView/1.0");
            }
            if (!out.has("Protocol-Version")) {
                out.put("Protocol-Version", "1.3");
            }
            out.put("webSocketDebuggerUrl", "ws://127.0.0.1:" + port + "/devtools/browser");
        } catch (Exception ignored) {
        }
        return out;
    }

    private JSONArray buildFilteredTargetsJson() throws IOException {
        JSONArray raw = fetchRawTargets();
        Set<String> allowed = manager.getRegisteredTargetIds();
        JSONArray filtered = new JSONArray();
        for (int i = 0; i < raw.length(); i++) {
            JSONObject item = raw.optJSONObject(i);
            if (item == null) {
                continue;
            }
            String id = item.optString("id", "");
            String url = item.optString("url", "");
            String type = item.optString("type", "page");
            if (!"page".equals(type)) {
                continue;
            }
            if (!allowed.contains(id) || manager.isMainUiUrl(url)) {
                continue;
            }
            try {
                JSONObject copy = new JSONObject(item.toString());
                copy.put("webSocketDebuggerUrl",
                        "ws://127.0.0.1:" + port + "/devtools/page/" + id);
                copy.remove("devtoolsFrontendUrl");
                filtered.put(copy);
            } catch (Exception ignored) {
            }
        }
        return filtered;
    }

    private JSONArray buildTargetInfosArray() throws IOException {
        JSONArray targets = buildFilteredTargetsJson();
        JSONArray infos = new JSONArray();
        for (int i = 0; i < targets.length(); i++) {
            infos.put(toTargetInfo(targets.optJSONObject(i)));
        }
        return infos;
    }

    private JSONObject toTargetInfo(JSONObject listEntry) {
        JSONObject info = new JSONObject();
        if (listEntry == null) {
            return info;
        }
        try {
            info.put("targetId", listEntry.optString("id", ""));
            info.put("type", listEntry.optString("type", "page"));
            info.put("title", listEntry.optString("title", ""));
            info.put("url", listEntry.optString("url", ""));
            info.put("attached", false);
            info.put("canAccessOpener", false);
        } catch (Exception ignored) {
        }
        return info;
    }

    private JSONObject findFilteredTargetById(String targetId) {
        if (TextUtils.isEmpty(targetId)) {
            return null;
        }
        try {
            JSONArray filtered = buildFilteredTargetsJson();
            for (int i = 0; i < filtered.length(); i++) {
                JSONObject obj = filtered.optJSONObject(i);
                if (obj != null && targetId.equals(obj.optString("id", ""))) {
                    return obj;
                }
            }
        } catch (IOException e) {
            Log.d(TAG, "Failed looking up filtered target " + targetId + ": " + e.getMessage());
        }
        return null;
    }

    private static RawHttpResponse fetchFromRawDevTools(String path) throws IOException {
        LocalSocket ls = null;
        try {
            ls = openRawDevToolsSocket();
            ls.setSoTimeout(5000);
            OutputStream out = ls.getOutputStream();
            String req = "GET " + path + " HTTP/1.1\r\n"
                    + "Host: localhost\r\n"
                    + "Connection: close\r\n\r\n";
            out.write(req.getBytes(StandardCharsets.US_ASCII));
            out.flush();

            InputStream in = ls.getInputStream();
            ByteArrayOutputStream raw = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int headerEnd = -1;
            int contentLength = -1;
            int statusCode = 200;

            while (true) {
                int n = in.read(buf);
                if (n < 0) {
                    break;
                }
                raw.write(buf, 0, n);
                byte[] cur = raw.toByteArray();
                if (headerEnd < 0) {
                    headerEnd = findDoubleCrlf(cur);
                    if (headerEnd >= 0) {
                        String hdr = new String(cur, 0, headerEnd, StandardCharsets.US_ASCII);
                        statusCode = parseStatusCode(hdr);
                        contentLength = parseContentLength(hdr);
                    }
                }
                if (headerEnd >= 0 && contentLength >= 0
                        && cur.length >= headerEnd + contentLength) {
                    break;
                }
            }

            byte[] all = raw.toByteArray();
            if (headerEnd < 0) {
                throw new IOException("Malformed HTTP response from raw DevTools socket");
            }
            int bodyLen = contentLength >= 0
                    ? Math.min(contentLength, all.length - headerEnd)
                    : (all.length - headerEnd);
            byte[] body = new byte[Math.max(0, bodyLen)];
            if (bodyLen > 0) {
                System.arraycopy(all, headerEnd, body, 0, bodyLen);
            }
            return new RawHttpResponse(statusCode, body);
        } finally {
            closeQuietly(ls);
        }
    }

    private static String extractNewTabUrl(String rawQuery) {
        if (TextUtils.isEmpty(rawQuery)) {
            return "about:blank";
        }
        String candidate = rawQuery;
        if (rawQuery.startsWith("url=")) {
            candidate = rawQuery.substring(4);
            int amp = candidate.indexOf('&');
            if (amp >= 0) {
                candidate = candidate.substring(0, amp);
            }
        }
        try {
            return URLDecoder.decode(candidate, "UTF-8");
        } catch (Exception e) {
            return candidate;
        }
    }

    private static String extractTargetIdSuffix(String path, String prefix, String rawQuery) {
        if (path.length() > prefix.length() + 1 && path.charAt(prefix.length()) == '/') {
            return path.substring(prefix.length() + 1);
        }
        if (!TextUtils.isEmpty(rawQuery)) {
            if (rawQuery.startsWith("id=")) {
                return rawQuery.substring(3);
            }
            return rawQuery;
        }
        return "";
    }

    private static String rpcResult(long id, JSONObject result) {
        try {
            JSONObject resp = new JSONObject();
            resp.put("id", id);
            resp.put("result", result);
            return resp.toString();
        } catch (Exception e) {
            return "{\"id\":" + id + ",\"result\":{}}";
        }
    }

    private static String rpcError(long id, int code, String message) {
        try {
            JSONObject err = new JSONObject();
            err.put("code", code);
            err.put("message", message);
            JSONObject resp = new JSONObject();
            resp.put("id", id);
            resp.put("error", err);
            return resp.toString();
        } catch (Exception e) {
            return "{\"id\":" + id + ",\"error\":{\"code\":" + code + ",\"message\":\"error\"}}";
        }
    }

    private static String computeWsAccept(String clientKey) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] digest = md.digest((clientKey + WS_GUID).getBytes(StandardCharsets.US_ASCII));
            return Base64.encodeToString(digest, Base64.NO_WRAP);
        } catch (Exception e) {
            throw new IllegalStateException("SHA-1 unavailable", e);
        }
    }

    private static WsFrame readWsFrame(InputStream in) throws IOException {
        ByteArrayOutputStream assembled = null;
        int firstOpcode = -1;

        while (true) {
            int b0 = in.read();
            if (b0 < 0) {
                return null;
            }
            int b1 = in.read();
            if (b1 < 0) {
                throw new EOFException();
            }

            boolean fin = (b0 & 0x80) != 0;
            int opcode = b0 & 0x0F;
            boolean masked = (b1 & 0x80) != 0;
            long len = b1 & 0x7F;
            if (len == 126) {
                len = ((readRequiredByte(in) & 0xFF) << 8) | (readRequiredByte(in) & 0xFF);
            } else if (len == 127) {
                len = 0;
                for (int i = 0; i < 8; i++) {
                    len = (len << 8) | (readRequiredByte(in) & 0xFFL);
                }
            }
            if (len > 32 * 1024 * 1024L) {
                throw new IOException("WebSocket frame too large: " + len);
            }

            byte[] mask = new byte[4];
            if (masked) {
                readFully(in, mask, 0, 4);
            }

            byte[] data = new byte[(int) len];
            readFully(in, data, 0, (int) len);
            if (masked) {
                for (int i = 0; i < data.length; i++) {
                    data[i] = (byte) (data[i] ^ mask[i & 3]);
                }
            }

            // Control frames may be interleaved between fragmented data frames
            if ((opcode & 0x08) != 0) {
                return new WsFrame(opcode, data);
            }

            if (firstOpcode < 0) {
                firstOpcode = opcode;
            }
            if (fin && assembled == null) {
                return new WsFrame(firstOpcode, data);
            }
            if (assembled == null) {
                assembled = new ByteArrayOutputStream();
            }
            assembled.write(data);
            if (fin) {
                return new WsFrame(firstOpcode, assembled.toByteArray());
            }
        }
    }

    private static void writeWsFrame(OutputStream out, Object lock, int opcode, byte[] payload)
            throws IOException {
        int len = payload != null ? payload.length : 0;
        ByteArrayOutputStream frame = new ByteArrayOutputStream(len + 10);
        frame.write(0x80 | (opcode & 0x0F));
        if (len < 126) {
            frame.write(len);
        } else if (len <= 0xFFFF) {
            frame.write(126);
            frame.write((len >> 8) & 0xFF);
            frame.write(len & 0xFF);
        } else {
            frame.write(127);
            long l = len;
            for (int shift = 56; shift >= 0; shift -= 8) {
                frame.write((int) ((l >> shift) & 0xFF));
            }
        }
        if (len > 0) {
            frame.write(payload);
        }
        synchronized (lock) {
            out.write(frame.toByteArray());
            out.flush();
        }
    }

    private static HttpRequest readHttpRequest(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int prev3 = -1, prev2 = -1, prev1 = -1;
        while (true) {
            int b = in.read();
            if (b < 0) {
                if (buf.size() == 0) {
                    return null;
                }
                break;
            }
            buf.write(b);
            if (prev3 == '\r' && prev2 == '\n' && prev1 == '\r' && b == '\n') {
                break;
            }
            if (buf.size() > 65536) {
                throw new IOException("HTTP header too large");
            }
            prev3 = prev2;
            prev2 = prev1;
            prev1 = b;
        }

        String rawHeader = new String(buf.toByteArray(), StandardCharsets.US_ASCII);
        String[] lines = rawHeader.split("\r\n");
        if (lines.length == 0 || lines[0].isEmpty()) {
            return null;
        }
        String[] parts = lines[0].split(" ");
        if (parts.length < 2) {
            return null;
        }

        String method = parts[0].trim();
        String target = parts[1].trim();
        String path = target;
        String rawQuery = "";
        int q = target.indexOf('?');
        if (q >= 0) {
            path = target.substring(0, q);
            rawQuery = target.substring(q + 1);
        }

        Map<String, String> headers = new HashMap<>();
        for (int i = 1; i < lines.length; i++) {
            String line = lines[i];
            int colon = line.indexOf(':');
            if (colon > 0) {
                String k = line.substring(0, colon).trim().toLowerCase();
                String v = line.substring(colon + 1).trim();
                headers.put(k, v);
            }
        }

        // Consume optional request body if Content-Length specified
        String clStr = headers.get("content-length");
        if (!TextUtils.isEmpty(clStr)) {
            try {
                int cl = Integer.parseInt(clStr);
                if (cl > 0 && cl <= 1024 * 1024) {
                    byte[] body = new byte[cl];
                    readFully(in, body, 0, cl);
                }
            } catch (NumberFormatException ignored) {
            }
        }

        return new HttpRequest(method, path, rawQuery, headers);
    }

    private static byte[] readUntilDoubleCrlf(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] tmp = new byte[4096];
        while (true) {
            int n = in.read(tmp);
            if (n < 0) {
                break;
            }
            out.write(tmp, 0, n);
            byte[] cur = out.toByteArray();
            if (findDoubleCrlf(cur) >= 0) {
                return cur;
            }
            if (cur.length > 65536) {
                return cur;
            }
        }
        return out.toByteArray();
    }

    private static int findDoubleCrlf(byte[] buf) {
        for (int i = 0; i <= buf.length - 4; i++) {
            if (buf[i] == '\r' && buf[i + 1] == '\n' && buf[i + 2] == '\r' && buf[i + 3] == '\n') {
                return i + 4;
            }
        }
        return -1;
    }

    private static int parseStatusCode(String header) {
        int sp1 = header.indexOf(' ');
        if (sp1 < 0) {
            return 200;
        }
        int sp2 = header.indexOf(' ', sp1 + 1);
        String codeStr = sp2 > sp1 ? header.substring(sp1 + 1, sp2) : header.substring(sp1 + 1).trim();
        try {
            return Integer.parseInt(codeStr.trim());
        } catch (NumberFormatException e) {
            return 200;
        }
    }

    private static int parseContentLength(String header) {
        for (String line : header.split("\r\n")) {
            int colon = line.indexOf(':');
            if (colon > 0 && "content-length".equalsIgnoreCase(line.substring(0, colon).trim())) {
                try {
                    return Integer.parseInt(line.substring(colon + 1).trim());
                } catch (NumberFormatException ignored) {
                }
            }
        }
        return -1;
    }

    private static void sendJsonResponse(OutputStream out, int code, String reason, String json)
            throws IOException {
        sendResponse(out, code, reason, "application/json; charset=utf-8",
                json.getBytes(StandardCharsets.UTF_8));
    }

    private static void sendTextResponse(OutputStream out, int code, String reason, String text)
            throws IOException {
        sendResponse(out, code, reason, "text/plain; charset=utf-8",
                text.getBytes(StandardCharsets.UTF_8));
    }

    private static void sendResponse(OutputStream out, int code, String reason, String contentType,
                                     byte[] body) throws IOException {
        String hdr = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Connection: close\r\n"
                + "Cache-Control: no-cache\r\n\r\n";
        out.write(hdr.getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }

    private static void pumpStream(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) >= 0) {
            if (n > 0) {
                out.write(buf, 0, n);
                out.flush();
            }
        }
    }

    private static int readRequiredByte(InputStream in) throws IOException {
        int b = in.read();
        if (b < 0) {
            throw new EOFException();
        }
        return b;
    }

    private static void readFully(InputStream in, byte[] dst, int off, int len) throws IOException {
        int remaining = len;
        int pos = off;
        while (remaining > 0) {
            int n = in.read(dst, pos, remaining);
            if (n < 0) {
                throw new EOFException();
            }
            pos += n;
            remaining -= n;
        }
    }

    private static void closeQuietly(Socket s) {
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static void closeQuietly(LocalSocket s) {
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static final class RawHttpResponse {
        final int statusCode;
        final byte[] body;

        RawHttpResponse(int statusCode, byte[] body) {
            this.statusCode = statusCode;
            this.body = body;
        }
    }

    private static final class HttpRequest {
        final String method;
        final String path;
        final String rawQuery;
        final Map<String, String> headers;

        HttpRequest(String method, String path, String rawQuery, Map<String, String> headers) {
            this.method = method;
            this.path = path;
            this.rawQuery = rawQuery;
            this.headers = headers;
        }

        String header(String lowerName) {
            return headers.get(lowerName);
        }
    }

    private static final class WsFrame {
        final int opcode;
        final byte[] payload;

        WsFrame(int opcode, byte[] payload) {
            this.opcode = opcode;
            this.payload = payload;
        }
    }
}
