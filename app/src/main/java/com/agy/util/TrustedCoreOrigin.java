package com.agy.util;

import android.net.Uri;

/** Exact origin for the embedded core UI; a loopback hostname alone is not trusted. */
public final class TrustedCoreOrigin {

    private TrustedCoreOrigin() {
    }

    public static boolean matches(Uri uri, int expectedPort) {
        return uri != null
                && "https".equalsIgnoreCase(uri.getScheme())
                && "127.0.0.1".equals(uri.getHost())
                && uri.getUserInfo() == null
                && uri.getPort() == expectedPort;
    }

    /** Loopback HTTP origin for sidecar/preview subframes (not valid for main-frame bridge trust). */
    public static boolean isLoopbackHttp(Uri uri) {
        if (uri == null || !"http".equalsIgnoreCase(uri.getScheme()) || uri.getUserInfo() != null) {
            return false;
        }
        String host = uri.getHost();
        return "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host);
    }
}
