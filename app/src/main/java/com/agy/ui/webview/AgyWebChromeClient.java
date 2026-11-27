package com.agy.ui.webview;

import android.net.Uri;
import android.os.Message;
import android.util.Log;
import android.webkit.ConsoleMessage;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebView;

import com.agy.util.SensitiveDataRedactor;

/** Routes console output to logcat, popups to PopupWindowHandler, file chooser, and WebView permission requests to callbacks. */
public final class AgyWebChromeClient extends WebChromeClient {

    private static final String TAG = "WebViewHost";

    public interface FileChooserCallback {
        boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback,
                                  FileChooserParams fileChooserParams);
    }

    public interface PermissionRequestCallback {
        void onPermissionRequest(PermissionRequest request);
    }

    private final PopupWindowHandler popupHandler;
    private final FileChooserCallback fileChooserCallback;
    private final PermissionRequestCallback permissionRequestCallback;

    public AgyWebChromeClient(PopupWindowHandler popupHandler, FileChooserCallback fileChooserCallback,
                              PermissionRequestCallback permissionRequestCallback) {
        this.popupHandler = popupHandler;
        this.fileChooserCallback = fileChooserCallback;
        this.permissionRequestCallback = permissionRequestCallback;
    }

    public AgyWebChromeClient(PopupWindowHandler popupHandler, FileChooserCallback fileChooserCallback) {
        this(popupHandler, fileChooserCallback, null);
    }

    public AgyWebChromeClient(PopupWindowHandler popupHandler) {
        this(popupHandler, null, null);
    }

    @Override
    public boolean onConsoleMessage(ConsoleMessage cm) {
        if (cm != null) {
            String msg = SensitiveDataRedactor.redactLine(cm.message());
            String src = SensitiveDataRedactor.safeUrl(cm.sourceId());
            int line = cm.lineNumber();
            ConsoleMessage.MessageLevel lvl = cm.messageLevel();
            String tag = "AGY_JS";
            if (lvl == ConsoleMessage.MessageLevel.ERROR) {
                Log.e(tag, "[" + src + ":" + line + "] " + msg);
            } else if (lvl == ConsoleMessage.MessageLevel.WARNING) {
                Log.w(tag, "[" + src + ":" + line + "] " + msg);
            } else {
                Log.i(tag, "[" + src + ":" + line + "] " + msg);
            }
        }
        return true;
    }

    @Override
    public boolean onCreateWindow(WebView parent, boolean isDialog, boolean isUserGesture,
                                  Message resultMsg) {
        return popupHandler.handle(parent, isDialog, isUserGesture, resultMsg);
    }

    @Override
    public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback,
                                      FileChooserParams fileChooserParams) {
        if (fileChooserCallback != null) {
            return fileChooserCallback.onShowFileChooser(webView, filePathCallback, fileChooserParams);
        }
        return super.onShowFileChooser(webView, filePathCallback, fileChooserParams);
    }

    @Override
    public void onPermissionRequest(PermissionRequest request) {
        if (permissionRequestCallback != null && request != null) {
            permissionRequestCallback.onPermissionRequest(request);
            return;
        }
        super.onPermissionRequest(request);
    }
}
