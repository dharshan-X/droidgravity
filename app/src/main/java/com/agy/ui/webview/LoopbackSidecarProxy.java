package com.agy.ui.webview;

import android.net.Uri;
import android.util.Log;
import android.webkit.WebResourceResponse;

import com.agy.util.MimeTypes;
import com.agy.util.SensitiveDataRedactor;
import com.agy.util.TrustedCoreOrigin;

import java.io.ByteArrayInputStream;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Proxies GET/HEAD requests to loopback HTTP sidecar gateways (http://localhost:<port>).
 *
 * <p>The main WebView document runs on {@code https://127.0.0.1:<corePort>}, while sidecar
 * gateways run on {@code http://localhost:<gatewayPort>}. Chromium treats that iframe as
 * cross-site and cross-scheme, withholding gateway session cookies on CSS/JS subresources
 * and causing {@code 401 Unauthorized} responses. Capturing each sidecar port's token and
 * session cookies here keeps CSS, JS, and GET API calls authenticated without exposing
 * credentials or touching non-sidecar dev servers.
 */
final class LoopbackSidecarProxy {

    private static final String TAG = "WebViewHost";
    private static final int CONNECT_TIMEOUT_MS = 3000;
    private static final int READ_TIMEOUT_MS = 10000;
    private static final int MAX_REDIRECTS = 3;

    private static final class PortAuth {
        volatile String token;
        final ConcurrentHashMap<String, String> cookies = new ConcurrentHashMap<>();
    }

    private final ConcurrentHashMap<Integer, PortAuth> authByPort = new ConcurrentHashMap<>();

    WebResourceResponse intercept(Uri uri, String method, Map<String, String> requestHeaders) {
        if (uri == null || !TrustedCoreOrigin.isLoopbackHttp(uri)) {
            return null;
        }
        if (!("GET".equalsIgnoreCase(method) || "HEAD".equalsIgnoreCase(method))) {
            return null;
        }
        int port = uri.getPort() > 0 ? uri.getPort() : 80;
        String queryToken = safeQueryParam(uri, "token");
        if (queryToken != null && !queryToken.isEmpty()) {
            PortAuth created = authByPort.get(port);
            if (created == null) {
                PortAuth fresh = new PortAuth();
                PortAuth existing = authByPort.putIfAbsent(port, fresh);
                created = existing != null ? existing : fresh;
            }
            if (!queryToken.equals(created.token)) {
                created.cookies.clear();
                created.token = queryToken;
            }
        }

        PortAuth auth = authByPort.get(port);
        if (auth == null) {
            return null;
        }

        try {
            return forwardWithRetry(uri, port, method.toUpperCase(Locale.US), requestHeaders, auth);
        } catch (Exception e) {
            Log.w(TAG, "sidecar proxy failed for " + SensitiveDataRedactor.safeUrl(uri.toString())
                    + ": " + e.getMessage());
            return null;
        }
    }

    private WebResourceResponse forwardWithRetry(
            Uri uri,
            int port,
            String method,
            Map<String, String> requestHeaders,
            PortAuth auth) throws IOException {
        Uri currentUri = uri;
        for (int hop = 0; hop <= MAX_REDIRECTS; hop++) {
            String hopToken = safeQueryParam(currentUri, "token");
            HttpURLConnection conn = openLoopback(currentUri, port, method, requestHeaders, auth, null);
            int status = conn.getResponseCode();
            captureCookies(conn, auth);

            if (status == HttpURLConnection.HTTP_UNAUTHORIZED
                    && (hopToken == null || hopToken.isEmpty())
                    && auth.token != null && !auth.token.isEmpty()) {
                conn.disconnect();
                conn = openLoopback(currentUri, port, method, requestHeaders, auth, auth.token);
                status = conn.getResponseCode();
                captureCookies(conn, auth);
            }

            if (isRedirect(status) && hop < MAX_REDIRECTS) {
                String location = conn.getHeaderField("Location");
                conn.disconnect();
                if (location == null || location.isEmpty()) {
                    return null;
                }
                Uri next = resolveRedirect(currentUri, location);
                if (!TrustedCoreOrigin.isLoopbackHttp(next)) {
                    return null;
                }
                currentUri = next;
                continue;
            }

            return toWebResourceResponse(conn, currentUri, method, status);
        }
        return null;
    }

    private HttpURLConnection openLoopback(
            Uri uri,
            int port,
            String method,
            Map<String, String> requestHeaders,
            PortAuth auth,
            String extraToken) throws IOException {
        Uri.Builder builder = uri.buildUpon()
                .scheme("http")
                .encodedAuthority("127.0.0.1:" + port);
        if (extraToken != null && !extraToken.isEmpty()) {
            builder.appendQueryParameter("token", extraToken);
        }
        URL url = new URL(builder.build().toString());
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod(method);
        conn.setInstanceFollowRedirects(false);
        conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
        conn.setReadTimeout(READ_TIMEOUT_MS);
        conn.setUseCaches(false);
        conn.setRequestProperty("Accept-Encoding", "identity");

        boolean hasCookie = false;
        boolean hasTokenHeader = false;
        if (requestHeaders != null) {
            for (Map.Entry<String, String> entry : requestHeaders.entrySet()) {
                String key = entry.getKey();
                String val = entry.getValue();
                if (key == null || val == null) {
                    continue;
                }
                if ("host".equalsIgnoreCase(key)
                        || "connection".equalsIgnoreCase(key)
                        || "accept-encoding".equalsIgnoreCase(key)
                        || "content-length".equalsIgnoreCase(key)) {
                    continue;
                }
                if ("cookie".equalsIgnoreCase(key)) {
                    hasCookie = true;
                } else if ("x-sidecar-token".equalsIgnoreCase(key)) {
                    hasTokenHeader = true;
                }
                conn.setRequestProperty(key, val);
            }
        }
        if (!hasCookie && !auth.cookies.isEmpty()) {
            conn.setRequestProperty("Cookie", buildCookieHeader(auth.cookies));
        }
        if (!hasTokenHeader && auth.token != null && !auth.token.isEmpty()) {
            conn.setRequestProperty("X-Sidecar-Token", auth.token);
        }
        return conn;
    }

    private static WebResourceResponse toWebResourceResponse(
            final HttpURLConnection conn, Uri uri, String method, int status) throws IOException {
        if (status < 100 || status >= 600) {
            conn.disconnect();
            return null;
        }
        String reason = conn.getResponseMessage();
        if (reason == null || reason.trim().isEmpty()) {
            reason = "OK";
        }

        String rawContentType = conn.getContentType();
        String mimeType = extractMimeType(rawContentType, uri);
        String encoding = extractCharset(rawContentType, mimeType);

        Map<String, String> headers = new HashMap<>();
        Map<String, List<String>> rawHeaders = conn.getHeaderFields();
        if (rawHeaders != null) {
            for (Map.Entry<String, List<String>> entry : rawHeaders.entrySet()) {
                String key = entry.getKey();
                List<String> values = entry.getValue();
                if (key == null || values == null || values.isEmpty()) {
                    continue;
                }
                if ("transfer-encoding".equalsIgnoreCase(key)
                        || "connection".equalsIgnoreCase(key)
                        || "keep-alive".equalsIgnoreCase(key)
                        || "content-encoding".equalsIgnoreCase(key)
                        || "set-cookie".equalsIgnoreCase(key)) {
                    continue;
                }
                headers.put(key, joinValues(values));
            }
        }

        if ("HEAD".equalsIgnoreCase(method)
                || status == HttpURLConnection.HTTP_NO_CONTENT
                || status == HttpURLConnection.HTTP_NOT_MODIFIED) {
            conn.disconnect();
            return new WebResourceResponse(
                    mimeType, encoding, status, reason, headers, new ByteArrayInputStream(new byte[0]));
        }

        InputStream rawStream = status >= HttpURLConnection.HTTP_BAD_REQUEST
                ? conn.getErrorStream()
                : conn.getInputStream();
        if (rawStream == null) {
            conn.disconnect();
            return new WebResourceResponse(
                    mimeType, encoding, status, reason, headers, new ByteArrayInputStream(new byte[0]));
        }
        InputStream wrapped = new FilterInputStream(rawStream) {
            @Override
            public void close() throws IOException {
                try {
                    super.close();
                } finally {
                    conn.disconnect();
                }
            }
        };
        return new WebResourceResponse(mimeType, encoding, status, reason, headers, wrapped);
    }

    private static void captureCookies(HttpURLConnection conn, PortAuth auth) {
        Map<String, List<String>> headers = conn.getHeaderFields();
        if (headers == null) {
            return;
        }
        for (Map.Entry<String, List<String>> entry : headers.entrySet()) {
            if (!"set-cookie".equalsIgnoreCase(entry.getKey()) || entry.getValue() == null) {
                continue;
            }
            for (String setCookie : entry.getValue()) {
                if (setCookie == null) {
                    continue;
                }
                int semi = setCookie.indexOf(';');
                String pair = semi >= 0 ? setCookie.substring(0, semi) : setCookie;
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    String name = pair.substring(0, eq).trim();
                    String value = pair.substring(eq + 1).trim();
                    if (!name.isEmpty()) {
                        auth.cookies.put(name, value);
                    }
                }
            }
        }
    }

    private static String buildCookieHeader(Map<String, String> cookies) {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> entry : cookies.entrySet()) {
            if (sb.length() > 0) {
                sb.append("; ");
            }
            sb.append(entry.getKey()).append('=').append(entry.getValue());
        }
        return sb.toString();
    }

    private static String extractMimeType(String rawContentType, Uri uri) {
        if (rawContentType != null && !rawContentType.isEmpty()) {
            int semi = rawContentType.indexOf(';');
            String type = (semi >= 0 ? rawContentType.substring(0, semi) : rawContentType)
                    .trim().toLowerCase(Locale.US);
            if (!type.isEmpty()) {
                return type;
            }
        }
        return MimeTypes.getMimeType(uri.getPath());
    }

    private static String extractCharset(String rawContentType, String mimeType) {
        if (rawContentType != null) {
            String[] parts = rawContentType.split(";");
            for (int i = 1; i < parts.length; i++) {
                String p = parts[i].trim();
                if (p.toLowerCase(Locale.US).startsWith("charset=")) {
                    String cs = p.substring("charset=".length()).trim();
                    if (cs.length() >= 2 && cs.startsWith("\"") && cs.endsWith("\"")) {
                        cs = cs.substring(1, cs.length() - 1);
                    }
                    if (!cs.isEmpty()) {
                        return cs;
                    }
                }
            }
        }
        return MimeTypes.isTextMime(mimeType) ? "UTF-8" : null;
    }

    private static String safeQueryParam(Uri uri, String key) {
        try {
            return uri.getQueryParameter(key);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static boolean isRedirect(int status) {
        return status == HttpURLConnection.HTTP_MOVED_PERM
                || status == HttpURLConnection.HTTP_MOVED_TEMP
                || status == HttpURLConnection.HTTP_SEE_OTHER
                || status == 307
                || status == 308;
    }

    private static Uri resolveRedirect(Uri base, String location) {
        try {
            Uri parsed = Uri.parse(location);
            if (parsed.getScheme() != null) {
                return parsed;
            }
            return Uri.parse(new URL(new URL(base.toString()), location).toString());
        } catch (Exception ignored) {
            return null;
        }
    }

    private static String joinValues(List<String> values) {
        if (values.size() == 1) {
            return values.get(0);
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(values.get(i));
        }
        return sb.toString();
    }
}
