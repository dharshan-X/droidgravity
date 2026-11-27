package com.agy.bridge;

import android.content.Context;
import android.webkit.JavascriptInterface;

import com.agy.runtime.RuntimeManager;
import com.agy.shell.ShizukuBridge;
import com.agy.theme.ThemeManager;
import com.agy.ui.MainActivity;

import java.lang.ref.WeakReference;
import java.nio.charset.Charset;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * Minimal, low-privilege JavaScript bridge for the app UI. It is visible to
 * embedded frames, so shell, notification, and arbitrary URL capabilities must
 * not be exposed here. Native-only getters below are intentionally unannotated.
 */
public final class AndroidBridge {

    private final Context appContext;
    private final UiBridge ui = new UiBridge();
    private final ThemeBridge themeBridge;
    private final EngineBridge engine;
    private final EngineConfigJson config;
    private final String blobBridgeToken = createBlobBridgeToken();
    private volatile WeakReference<MainActivity> activityRef;

    public AndroidBridge(Context context, RuntimeManager runtime,
                         ShizukuBridge shizuku, ThemeManager theme) {
        this.appContext = context.getApplicationContext();
        this.themeBridge = new ThemeBridge(theme);
        this.engine = new EngineBridge(runtime);
        this.config = new EngineConfigJson(this.appContext, runtime, engine, shizuku, themeBridge);
    }

    /** Host activity, used to dismiss the native splash once the UI has painted. */
    public void setActivity(MainActivity activity) {
        this.activityRef = activity != null ? new WeakReference<MainActivity>(activity) : null;
        ui.setActivity(activity);
    }

    @JavascriptInterface
    public void onUiReady(String token) {
        if (matchesNativeBridgeToken(token)) {
            ui.onUiReady();
        }
    }

    @JavascriptInterface
    public void openDirectoryPicker() {
        final MainActivity act = activityRef != null ? activityRef.get() : MainActivity.activeActivity;
        if (act != null) {
            act.runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    act.launchDirectoryPicker();
                }
            });
        }
    }

    @JavascriptInterface
    public void onUiError(String token, final String errorMessage) {
        if (matchesNativeBridgeToken(token)) {
            ui.onUiError(errorMessage);
        }
    }

    @JavascriptInterface
    public boolean setTheme(String mode) {
        return themeBridge.setTheme(mode);
    }

    @JavascriptInterface
    public boolean setThemeByUser(final String mode) {
        return themeBridge.setThemeByUser(mode);
    }

    @JavascriptInterface
    public void onVisualThemeChanged(String visual) {
        themeBridge.onVisualThemeChanged(visual);
    }

    @JavascriptInterface
    public String getTheme() {
        return themeBridge.getTheme();
    }

    @JavascriptInterface
    public boolean isDesktopMode() {
        return ui.isDesktopMode();
    }

    @JavascriptInterface
    public void setDesktopMode(boolean enabled) {
        ui.setDesktopMode(enabled);
    }

    // Native-only helpers used while preparing the trusted app document.
    public boolean isEngineReady() {
        return engine.isReady();
    }

    public int getEnginePort() {
        return engine.getPort();
    }

    public String getEngineBaseUrl() {
        return engine.getBaseUrl();
    }

    public String getCsrfToken() {
        return engine.getCsrfToken();
    }

    public String getConfig() {
        return config.get();
    }

    /** Native-only token inserted into the trusted top-level document. */
    public String getBlobBridgeToken() {
        return blobBridgeToken;
    }

    /** Saves a downloaded blob only for scripts holding the top-level document token. */
    @JavascriptInterface
    public void saveBlob(String token, String base64Data, String filename, String mimeType) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        final MainActivity act = activityRef != null ? activityRef.get() : MainActivity.activeActivity;
        if (act != null) {
            com.agy.ui.download.FileDownloader.handleBlobData(
                    act, base64Data, filename, mimeType);
        }
    }

    @JavascriptInterface
    public String wcvAcquire(String token, String tabKey, String initialUrl) {
        if (!matchesNativeBridgeToken(token)) {
            return "{}";
        }
        return com.agy.browser.AgentBrowserManager.getInstance(appContext)
                .wcvAcquire(tabKey, initialUrl).toString();
    }

    @JavascriptInterface
    public String wcvShow(String token, String tabKey, int leftX100, int topX100,
                          int widthX100, int heightX100, int vpWidthX100, int vpHeightX100) {
        if (!matchesNativeBridgeToken(token)) {
            return "{}";
        }
        return com.agy.browser.AgentBrowserManager.getInstance(appContext)
                .wcvShow(tabKey, leftX100, topX100, widthX100, heightX100, vpWidthX100, vpHeightX100)
                .toString();
    }

    @JavascriptInterface
    public void wcvSetBounds(String token, String tabKey, int leftX100, int topX100,
                             int widthX100, int heightX100, int vpWidthX100, int vpHeightX100) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext)
                .wcvSetBounds(tabKey, leftX100, topX100, widthX100, heightX100, vpWidthX100, vpHeightX100);
    }

    @JavascriptInterface
    public void wcvHide(String token, String tabKey) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext).wcvHide(tabKey);
    }

    @JavascriptInterface
    public void wcvSetOverlayActive(String token, String tabKey, boolean active) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext)
                .wcvSetOverlayActive(tabKey, active);
    }

    @JavascriptInterface
    public void wcvNavigate(String token, String tabKey, String url) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext).wcvNavigate(tabKey, url);
    }

    @JavascriptInterface
    public void wcvControl(String token, String tabKey, String action) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext).wcvControl(tabKey, action);
    }

    @JavascriptInterface
    public void wcvDispose(String token, String tabKey) {
        if (!matchesNativeBridgeToken(token)) {
            return;
        }
        com.agy.browser.AgentBrowserManager.getInstance(appContext).wcvDispose(tabKey);
    }

    private boolean matchesNativeBridgeToken(String candidate) {
        if (candidate == null) {
            return false;
        }
        try {
            byte[] expected = blobBridgeToken.getBytes(Charset.forName("UTF-8"));
            byte[] actual = candidate.getBytes(Charset.forName("UTF-8"));
            return MessageDigest.isEqual(expected, actual);
        } catch (Exception ignored) {
            return false;
        }
    }

    private static String createBlobBridgeToken() {
        byte[] random = new byte[32];
        new SecureRandom().nextBytes(random);
        char[] hex = "0123456789abcdef".toCharArray();
        char[] encoded = new char[random.length * 2];
        for (int i = 0; i < random.length; i++) {
            int value = random[i] & 0xff;
            encoded[i * 2] = hex[value >>> 4];
            encoded[i * 2 + 1] = hex[value & 0x0f];
        }
        return new String(encoded);
    }
}
