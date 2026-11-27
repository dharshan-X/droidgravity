package com.agy.browser;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.MutableContextWrapper;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.text.TextUtils;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.ViewParent;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.agy.runtime.RuntimeConfig;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Manages dedicated, isolated {@link WebView} tabs for Antigravity browser agents.
 *
 * <p>Android exposes all {@code WebView} instances in the process on a single
 * {@code @webview_devtools_remote_<pid>} abstract Unix socket, including the main
 * Antigravity UI WebView. This manager maintains an explicit allowlist of agent
 * CDP target IDs so {@link CdpProxyServer} never leaks or controls the UI WebView.
 *
 * <p>When collapsed, the agent WebView container sits at index 0 of {@code MainActivity}'s
 * root layout (visible with valid window dimensions behind the opaque main UI WebView)
 * so Chromium's compositor keeps rendering for {@code Page.captureScreenshot} and input
 * events while remaining invisible and non-focusable to the user. Tapping the compact
 * floating pill or calling {@code cdp_tab show} brings the viewer overlay to the front.
 */
public final class AgentBrowserManager {

    private static final String TAG = "AgentBrowser";
    private static final String MARKER_QUERY_KEY = "__agy_tab=";
    private static final long TARGET_DISCOVERY_TIMEOUT_MS = 4000L;
    private static final long TARGET_POLL_INTERVAL_MS = 25L;

    private static volatile AgentBrowserManager instance;

    private final Context appContext;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object tabLock = new Object();
    private final Object createLock = new Object();
    private final AtomicInteger tokenSeq = new AtomicInteger(0);
    private final ScheduledExecutorService bgExecutor = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "agy-agent-browser");
        t.setDaemon(true);
        return t;
    });

    /** Registered agent tabs ordered by creation, keyed by CDP target ID. */
    private final Map<String, AgentTab> tabsByCdpId = new LinkedHashMap<>();
    /** Side Panel WCV tabs keyed by Antigravity tabKey (e.g. "browser__..."). */
    private final Map<String, AgentTab> tabsByKey = new LinkedHashMap<>();
    private final Set<String> pendingMarkerUrls = new HashSet<>();

    private volatile int cdpProxyPort = 0;
    private volatile String selectedTargetId = null;
    private volatile boolean expanded = false;
    private volatile boolean debuggingEnabled = false;

    private volatile boolean wcvVisible = false;
    private volatile boolean wcvOverlayActive = false;
    private volatile String activeWcvTabKey = null;
    private volatile int wcvLeftPx = 0;
    private volatile int wcvTopPx = 0;
    private volatile int wcvWidthPx = 0;
    private volatile int wcvHeightPx = 0;

    private WeakReference<Activity> activityRef = new WeakReference<>(null);
    private WeakReference<FrameLayout> hostRootRef = new WeakReference<>(null);
    private WeakReference<WebView> uiWebViewRef = new WeakReference<>(null);

    private MutableContextWrapper overlayContext;
    private FrameLayout agentContainer;
    private LinearLayout toolbarView;
    private TextView urlTitleView;
    private TextView tabBadgeView;
    private FrameLayout webViewStage;
    private TextView floatingPill;

    public static final class PendingDialog {
        public final String type;
        public final String message;
        public final String defaultPrompt;
        final JsResult result;

        PendingDialog(String type, String message, String defaultPrompt, JsResult result) {
            this.type = type;
            this.message = message;
            this.defaultPrompt = defaultPrompt;
            this.result = result;
        }
    }

    static final class AgentTab {
        final String token;
        final int webContentsId;
        String tabKey;
        String cdpTargetId;
        WebView webView;
        volatile String currentUrl = "about:blank";
        volatile String currentTitle = "";
        volatile boolean loading = false;
        volatile boolean domReady = false;
        volatile PendingDialog pendingDialog = null;

        AgentTab(String token, int webContentsId, WebView webView) {
            this.token = token;
            this.webContentsId = webContentsId;
            this.webView = webView;
        }
    }

    private AgentBrowserManager(Context context) {
        this.appContext = context.getApplicationContext();
    }

    public static AgentBrowserManager getInstance(Context context) {
        if (instance == null) {
            synchronized (AgentBrowserManager.class) {
                if (instance == null) {
                    instance = new AgentBrowserManager(context);
                }
            }
        }
        return instance;
    }

    public void setCdpProxyPort(int port) {
        this.cdpProxyPort = port;
    }

    public int getCdpProxyPort() {
        return cdpProxyPort;
    }

    /**
     * Ensures WebView remote debugging is enabled process-wide.
     * Safe to call from any thread; runs on the main looper.
     */
    public void ensureDebuggingEnabled() {
        if (debuggingEnabled) {
            return;
        }
        runOnMainSync(() -> {
            if (!debuggingEnabled && Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
                WebView.setWebContentsDebuggingEnabled(true);
                debuggingEnabled = true;
            }
        });
    }

    /**
     * Attaches the agent browser overlay views to {@link android.app.Activity}'s root container.
     * Safe to call repeatedly across {@code onCreate} / {@code onResume}.
     */
    public void attachActivity(Activity activity, FrameLayout hostRoot) {
        attachActivity(activity, hostRoot, null);
    }

    public void attachActivity(Activity activity, FrameLayout hostRoot, WebView uiWebView) {
        if (activity == null || hostRoot == null) {
            return;
        }
        runOnMain(() -> {
            activityRef = new WeakReference<>(activity);
            hostRootRef = new WeakReference<>(hostRoot);
            if (uiWebView != null) {
                uiWebViewRef = new WeakReference<>(uiWebView);
            }
            ensureViewsCreated(activity);
            if (overlayContext != null) {
                overlayContext.setBaseContext(activity);
            }
            mountViewsIntoHost(hostRoot);
            syncOverlayStateOnMain();
        });
    }

    /**
     * Detaches the activity reference without destroying active agent tabs.
     */
    public void detachActivity(Activity activity) {
        runOnMain(() -> {
            Activity current = activityRef.get();
            if (current != null && current != activity) {
                return;
            }
            activityRef = new WeakReference<>(null);
            hostRootRef = new WeakReference<>(null);
            uiWebViewRef = new WeakReference<>(null);
            wcvVisible = false;
            wcvOverlayActive = false;
            if (overlayContext != null) {
                overlayContext.setBaseContext(appContext);
            }
            detachFromParent(agentContainer);
            detachFromParent(floatingPill);
        });
    }

    public boolean isExpanded() {
        return expanded;
    }

    public void expand() {
        runOnMain(() -> {
            expanded = true;
            syncOverlayStateOnMain();
        });
    }

    public void collapse() {
        runOnMain(() -> {
            expanded = false;
            syncOverlayStateOnMain();
        });
    }

    /**
     * Handles back navigation when the interactive agent browser sheet is open.
     *
     * @return {@code true} if the back press was consumed by the agent browser overlay.
     */
    public boolean handleBackPressed() {
        if (!expanded) {
            return false;
        }
        AgentTab selected = getSelectedTab();
        if (selected != null && selected.webView != null && selected.webView.canGoBack()) {
            selected.webView.goBack();
            return true;
        }
        collapse();
        return true;
    }

    /**
     * Returns an immutable snapshot of all registered agent CDP target IDs.
     */
    public Set<String> getRegisteredTargetIds() {
        synchronized (tabLock) {
            return new HashSet<>(tabsByCdpId.keySet());
        }
    }

    /**
     * Returns true strictly when {@code targetId} belongs to an agent-owned WebView tab
     * and its URL is not the local Antigravity server origin.
     */
    public boolean isAgentTargetAllowed(String targetId, String url) {
        if (TextUtils.isEmpty(targetId)) {
            return false;
        }
        if (isMainUiUrl(url)) {
            return false;
        }
        synchronized (tabLock) {
            return tabsByCdpId.containsKey(targetId);
        }
    }

    public boolean isMainUiUrl(String url) {
        if (TextUtils.isEmpty(url)) {
            return false;
        }
        int httpsPort = RuntimeConfig.httpsPort(appContext);
        String p = String.valueOf(httpsPort);
        return url.startsWith("https://127.0.0.1:" + p)
                || url.startsWith("http://127.0.0.1:" + p)
                || url.startsWith("https://localhost:" + p)
                || url.startsWith("http://localhost:" + p);
    }

    public String getSelectedTargetId() {
        synchronized (tabLock) {
            if (selectedTargetId != null && tabsByCdpId.containsKey(selectedTargetId)) {
                return selectedTargetId;
            }
            if (!tabsByCdpId.isEmpty()) {
                selectedTargetId = tabsByCdpId.keySet().iterator().next();
                return selectedTargetId;
            }
            return null;
        }
    }

    public int getTabCount() {
        synchronized (tabLock) {
            return tabsByCdpId.size();
        }
    }

    /**
     * Creates a new dedicated agent {@link WebView} tab, binds it to its CDP target ID from
     * {@code @webview_devtools_remote_<pid>}, and optionally navigates to {@code initialUrl}.
     *
     * @return the newly bound CDP target ID, or {@code null} if creation/discovery failed.
     */
    public String createTab(String initialUrl) {
        AgentTab tab = createTabInternal(initialUrl, null, true);
        return tab != null ? tab.cdpTargetId : null;
    }

    private AgentTab createTabInternal(String initialUrl, String explicitTabKey, boolean notifySidePanel) {
        ensureDebuggingEnabled();
        String targetUrl = normalizeRequestedUrl(initialUrl);

        synchronized (createLock) {
            Set<String> knownNonAgentIds = snapshotUnknownTargetIds();
            int seq = tokenSeq.incrementAndGet();
            String token = "agy-" + seq;
            String markerUrl = "about:blank?" + MARKER_QUERY_KEY + token;
            pendingMarkerUrls.add(markerUrl);

            final AtomicReference<AgentTab> createdRef = new AtomicReference<>();
            runOnMainSync(() -> {
                ensureViewsCreated(activityRef.get());
                AgentTab tab = createTabViewOnMain(token, seq, null);
                tab.tabKey = explicitTabKey;
                createdRef.set(tab);
                tab.webView.loadUrl(markerUrl);
            });

            AgentTab tab = createdRef.get();
            if (tab == null) {
                pendingMarkerUrls.remove(markerUrl);
                return null;
            }

            String cdpId = awaitNewCdpTargetId(knownNonAgentIds, markerUrl);
            pendingMarkerUrls.remove(markerUrl);

            if (TextUtils.isEmpty(cdpId)) {
                Log.w(TAG, "Timed out waiting for CDP target registration for " + token);
                runOnMain(() -> destroyTabViewOnMain(tab));
                return null;
            }

            tab.cdpTargetId = cdpId;
            if (TextUtils.isEmpty(tab.tabKey)) {
                tab.tabKey = "browser__cdp_" + cdpId;
            }
            synchronized (tabLock) {
                tabsByCdpId.put(cdpId, tab);
                tabsByKey.put(tab.tabKey, tab);
                selectedTargetId = cdpId;
            }

            runOnMain(() -> {
                tab.currentUrl = targetUrl;
                tab.webView.loadUrl(targetUrl);
                syncOverlayStateOnMain();
                if (notifySidePanel) {
                    emitWcvHostEvent("agent-tab-created", tab, null);
                }
            });

            return tab;
        }
    }

    /**
     * Closes the agent tab identified by {@code cdpTargetId}.
     */
    public boolean closeTab(String cdpTargetId) {
        if (TextUtils.isEmpty(cdpTargetId)) {
            return false;
        }
        final AgentTab removed;
        synchronized (tabLock) {
            removed = tabsByCdpId.remove(cdpTargetId);
            if (removed == null) {
                return false;
            }
            if (!TextUtils.isEmpty(removed.tabKey)) {
                tabsByKey.remove(removed.tabKey);
                if (removed.tabKey.equals(activeWcvTabKey)) {
                    wcvVisible = false;
                    activeWcvTabKey = null;
                }
            }
            if (cdpTargetId.equals(selectedTargetId)) {
                selectedTargetId = tabsByCdpId.isEmpty()
                        ? null
                        : tabsByCdpId.keySet().iterator().next();
            }
        }
        runOnMain(() -> {
            emitWcvHostEvent("agent-tab-closed", removed, null);
            destroyTabViewOnMain(removed);
            if (getTabCount() == 0) {
                expanded = false;
                wcvVisible = false;
            }
            syncOverlayStateOnMain();
        });
        return true;
    }

    /**
     * Selects the given agent tab as active in the viewer stage.
     */
    public boolean activateTab(String cdpTargetId) {
        if (TextUtils.isEmpty(cdpTargetId)) {
            return false;
        }
        synchronized (tabLock) {
            if (!tabsByCdpId.containsKey(cdpTargetId)) {
                return false;
            }
            selectedTargetId = cdpTargetId;
        }
        runOnMain(this::syncOverlayStateOnMain);
        return true;
    }

    /**
     * Resolves a pending JavaScript dialog (`alert`, `confirm`, `prompt`) on an agent tab.
     */
    public JSONObject handlePendingDialog(String cdpTargetId, boolean accept, String promptText) {
        JSONObject out = new JSONObject();
        try {
            AgentTab tab = resolveTab(cdpTargetId);
            if (tab == null || tab.pendingDialog == null) {
                out.put("ok", false);
                out.put("error", "no pending dialog");
                return out;
            }
            PendingDialog dialog = tab.pendingDialog;
            tab.pendingDialog = null;
            runOnMainSync(() -> {
                if (dialog.result instanceof JsPromptResult && accept && promptText != null) {
                    ((JsPromptResult) dialog.result).confirm(promptText);
                } else if (accept) {
                    dialog.result.confirm();
                } else {
                    dialog.result.cancel();
                }
            });
            out.put("ok", true);
            out.put("type", dialog.type);
            out.put("message", dialog.message);
        } catch (Exception e) {
            try {
                out.put("ok", false);
                out.put("error", e.getMessage());
            } catch (Exception ignored) {
            }
        }
        return out;
    }

    /**
     * Returns current CDP proxy status and open agent tabs as JSON.
     */
    public JSONObject getStatusJson() {
        JSONObject res = new JSONObject();
        try {
            res.put("ok", true);
            res.put("cdpPort", cdpProxyPort);
            res.put("expanded", expanded);
            res.put("selectedTargetId", getSelectedTargetId());
            JSONArray tabs = new JSONArray();
            synchronized (tabLock) {
                for (AgentTab tab : tabsByCdpId.values()) {
                    JSONObject item = new JSONObject();
                    item.put("id", tab.cdpTargetId);
                    item.put("url", tab.currentUrl);
                    item.put("title", tab.currentTitle);
                    item.put("loading", tab.loading);
                    item.put("selected", tab.cdpTargetId != null && tab.cdpTargetId.equals(selectedTargetId));
                    if (tab.pendingDialog != null) {
                        JSONObject dlg = new JSONObject();
                        dlg.put("type", tab.pendingDialog.type);
                        dlg.put("message", tab.pendingDialog.message);
                        item.put("pendingDialog", dlg);
                    }
                    tabs.put(item);
                }
            }
            res.put("tabCount", tabs.length());
            res.put("tabs", tabs);
        } catch (Exception e) {
            Log.w(TAG, "Failed building status JSON", e);
        }
        return res;
    }

    /**
     * Handles {@code cdp_tab} commands sent over {@link com.agy.gateway.SystemGatewayServer}.
     */
    public JSONObject handleGatewayTabCommand(JSONObject payload) {
        String action = payload != null ? payload.optString("action", "list") : "list";
        String targetId = payload != null ? payload.optString("targetId", "") : "";
        String url = payload != null ? payload.optString("url", "about:blank") : "about:blank";
        JSONObject out = new JSONObject();
        try {
            switch (action) {
                case "list":
                    return getStatusJson();
                case "new": {
                    String created = createTab(url);
                    out.put("ok", created != null);
                    if (created != null) {
                        out.put("targetId", created);
                    } else {
                        out.put("error", "failed to create agent browser tab");
                    }
                    return out;
                }
                case "close": {
                    boolean closed = closeTab(targetId);
                    out.put("ok", closed);
                    if (!closed) {
                        out.put("error", "tab not found: " + targetId);
                    }
                    return out;
                }
                case "activate": {
                    boolean activated = activateTab(targetId);
                    out.put("ok", activated);
                    if (!activated) {
                        out.put("error", "tab not found: " + targetId);
                    }
                    return out;
                }
                case "show":
                    expand();
                    out.put("ok", true);
                    out.put("expanded", true);
                    return out;
                case "hide":
                    collapse();
                    out.put("ok", true);
                    out.put("expanded", false);
                    return out;
                case "navigate": {
                    AgentTab tab = resolveTab(targetId);
                    if (tab == null) {
                        out.put("ok", false);
                        out.put("error", "no matching agent tab");
                        return out;
                    }
                    String navUrl = normalizeRequestedUrl(url);
                    runOnMain(() -> tab.webView.loadUrl(navUrl));
                    out.put("ok", true);
                    out.put("targetId", tab.cdpTargetId);
                    out.put("url", navUrl);
                    return out;
                }
                case "dialog": {
                    boolean accept = payload == null || payload.optBoolean("accept", true);
                    String promptText = payload != null && payload.has("promptText")
                            ? payload.optString("promptText", "") : null;
                    return handlePendingDialog(targetId, accept, promptText);
                }
                default:
                    out.put("ok", false);
                    out.put("error", "unknown cdp_tab action: " + action);
                    return out;
            }
        } catch (Exception e) {
            try {
                out.put("ok", false);
                out.put("error", e.getMessage());
            } catch (Exception ignored) {
            }
            return out;
        }
    }

    // -------------------------------------------------------------------------
    // Side Panel WebContentsView (WCV) Bridge API
    // -------------------------------------------------------------------------

    public JSONObject wcvAcquire(String tabKey, String initialUrl) {
        AgentTab tab = resolveTabByKey(tabKey);
        if (tab == null) {
            tab = createTabInternal(
                    TextUtils.isEmpty(initialUrl) ? "about:blank" : initialUrl,
                    tabKey,
                    false);
        }
        return buildWcvTabState(tab);
    }

    public JSONObject wcvShow(String tabKey, int leftPx, int topPx, int widthPx, int heightPx) {
        AgentTab tab = resolveTabByKey(tabKey);
        if (tab != null) {
            synchronized (tabLock) {
                selectedTargetId = tab.cdpTargetId;
            }
            activeWcvTabKey = tab.tabKey;
        } else {
            activeWcvTabKey = tabKey;
        }
        wcvLeftPx = Math.max(0, leftPx);
        wcvTopPx = Math.max(0, topPx);
        wcvWidthPx = Math.max(0, widthPx);
        wcvHeightPx = Math.max(0, heightPx);
        wcvVisible = true;
        runOnMain(this::syncOverlayStateOnMain);
        return buildWcvTabState(tab);
    }

    public void wcvSetBounds(String tabKey, int leftPx, int topPx, int widthPx, int heightPx) {
        if (!TextUtils.isEmpty(tabKey) && activeWcvTabKey != null && !tabKey.equals(activeWcvTabKey)) {
            return;
        }
        wcvLeftPx = Math.max(0, leftPx);
        wcvTopPx = Math.max(0, topPx);
        wcvWidthPx = Math.max(0, widthPx);
        wcvHeightPx = Math.max(0, heightPx);
        runOnMain(this::syncOverlayStateOnMain);
    }

    public void wcvHide(String tabKey) {
        if (TextUtils.isEmpty(tabKey) || tabKey.equals(activeWcvTabKey)) {
            wcvVisible = false;
            runOnMain(this::syncOverlayStateOnMain);
        }
    }

    public void wcvSetOverlayActive(String tabKey, boolean active) {
        if (TextUtils.isEmpty(tabKey) || tabKey.equals(activeWcvTabKey)) {
            wcvOverlayActive = active;
            runOnMain(this::syncOverlayStateOnMain);
        }
    }

    public void wcvNavigate(String tabKey, String url) {
        AgentTab tab = resolveTabByKey(tabKey);
        if (tab == null || tab.webView == null) {
            return;
        }
        String navUrl = normalizeRequestedUrl(url);
        runOnMain(() -> {
            if (tab.webView != null) {
                tab.currentUrl = navUrl;
                tab.webView.loadUrl(navUrl);
            }
        });
    }

    public void wcvControl(String tabKey, String action) {
        AgentTab tab = resolveTabByKey(tabKey);
        if (tab == null || tab.webView == null || TextUtils.isEmpty(action)) {
            return;
        }
        runOnMain(() -> {
            WebView wv = tab.webView;
            if (wv == null) {
                return;
            }
            switch (action) {
                case "back":
                    if (wv.canGoBack()) {
                        wv.goBack();
                    }
                    break;
                case "forward":
                    if (wv.canGoForward()) {
                        wv.goForward();
                    }
                    break;
                case "reload":
                    wv.reload();
                    break;
                case "stop":
                    wv.stopLoading();
                    break;
                default:
                    break;
            }
        });
    }

    public void wcvDispose(String tabKey) {
        AgentTab tab = resolveTabByKey(tabKey);
        if (tab != null && !TextUtils.isEmpty(tab.cdpTargetId)) {
            closeTab(tab.cdpTargetId);
        }
    }

    private AgentTab resolveTabByKey(String tabKey) {
        if (TextUtils.isEmpty(tabKey)) {
            return null;
        }
        synchronized (tabLock) {
            AgentTab direct = tabsByKey.get(tabKey);
            if (direct != null) {
                return direct;
            }
            // Strip Side Panel prefixes like "builtin-browser:<scope>:browser__cdp_<id>" or "browser__cdp_<id>"
            int cdpIdx = tabKey.indexOf("browser__cdp_");
            if (cdpIdx >= 0) {
                String tail = tabKey.substring(cdpIdx + "browser__cdp_".length());
                int colon = tail.indexOf(':');
                String cdpId = colon >= 0 ? tail.substring(0, colon) : tail;
                AgentTab byCdp = tabsByCdpId.get(cdpId);
                if (byCdp != null) {
                    byCdp.tabKey = tabKey;
                    tabsByKey.put(tabKey, byCdp);
                    return byCdp;
                }
            }
            if (tabsByCdpId.containsKey(tabKey)) {
                AgentTab byCdp = tabsByCdpId.get(tabKey);
                if (byCdp != null) {
                    byCdp.tabKey = tabKey;
                    tabsByKey.put(tabKey, byCdp);
                }
                return byCdp;
            }
            return null;
        }
    }

    private JSONObject buildWcvTabState(AgentTab tab) {
        JSONObject out = new JSONObject();
        if (tab == null) {
            return out;
        }
        try {
            out.put("webContentsId", tab.webContentsId);
            out.put("targetId", tab.cdpTargetId != null ? tab.cdpTargetId : "");
            out.put("tabKey", tab.tabKey != null ? tab.tabKey : "");
            out.put("url", tab.currentUrl != null ? tab.currentUrl : "about:blank");
            out.put("title", tab.currentTitle != null ? tab.currentTitle : "");
            out.put("isLoading", tab.loading);
            final boolean[] navFlags = {false, false};
            if (tab.webView != null) {
                runOnMainSync(() -> {
                    if (tab.webView != null) {
                        navFlags[0] = tab.webView.canGoBack();
                        navFlags[1] = tab.webView.canGoForward();
                    }
                });
            }
            out.put("canGoBack", navFlags[0]);
            out.put("canGoForward", navFlags[1]);
        } catch (Exception ignored) {
        }
        return out;
    }

    private void emitWcvHostEvent(String type, AgentTab tab, JSONObject extra) {
        WebView uiWv = uiWebViewRef.get();
        if (uiWv == null || tab == null) {
            return;
        }
        try {
            JSONObject ev = extra != null ? new JSONObject(extra.toString()) : new JSONObject();
            ev.put("type", type);
            ev.put("tabKey", tab.tabKey != null ? tab.tabKey : "");
            ev.put("targetId", tab.cdpTargetId != null ? tab.cdpTargetId : "");
            ev.put("webContentsId", tab.webContentsId);
            ev.put("url", tab.currentUrl != null ? tab.currentUrl : "about:blank");
            ev.put("title", tab.currentTitle != null ? tab.currentTitle : "");
            ev.put("isLoading", tab.loading);
            if (tab.webView != null) {
                ev.put("canGoBack", tab.webView.canGoBack());
                ev.put("canGoForward", tab.webView.canGoForward());
            }
            final String js = "try{window.__agWcvHost&&window.__agWcvHost.onEvent("
                    + ev.toString() + ");}catch(_){}";
            runOnMain(() -> {
                WebView target = uiWebViewRef.get();
                if (target != null) {
                    target.evaluateJavascript(js, null);
                }
            });
        } catch (Exception ignored) {
        }
    }

    /**
     * Destroys all active agent WebView tabs cleanly. Called on service shutdown.
     */
    public void destroyAll() {
        final List<AgentTab> snapshot;
        synchronized (tabLock) {
            snapshot = new ArrayList<>(tabsByCdpId.values());
            tabsByCdpId.clear();
            tabsByKey.clear();
            selectedTargetId = null;
        }
        expanded = false;
        wcvVisible = false;
        wcvOverlayActive = false;
        activeWcvTabKey = null;
        runOnMain(() -> {
            for (AgentTab tab : snapshot) {
                destroyTabViewOnMain(tab);
            }
            syncOverlayStateOnMain();
        });
    }

    private Set<String> snapshotUnknownTargetIds() {
        Set<String> existing = new HashSet<>();
        try {
            JSONArray targets = CdpProxyServer.fetchRawTargets();
            if (targets != null) {
                for (int i = 0; i < targets.length(); i++) {
                    JSONObject obj = targets.optJSONObject(i);
                    if (obj != null) {
                        String id = obj.optString("id", "");
                        if (!id.isEmpty()) {
                            existing.add(id);
                        }
                    }
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "Raw target snapshot unavailable: " + e.getMessage());
        }
        return existing;
    }

    private String awaitNewCdpTargetId(Set<String> beforeIds, String markerUrl) {
        long deadline = System.currentTimeMillis() + TARGET_DISCOVERY_TIMEOUT_MS;
        while (System.currentTimeMillis() < deadline) {
            try {
                JSONArray targets = CdpProxyServer.fetchRawTargets();
                if (targets != null) {
                    String fallbackCandidate = null;
                    for (int i = 0; i < targets.length(); i++) {
                        JSONObject t = targets.optJSONObject(i);
                        if (t == null) {
                            continue;
                        }
                        String type = t.optString("type", "page");
                        if (!"page".equals(type)) {
                            continue;
                        }
                        String id = t.optString("id", "");
                        String url = t.optString("url", "");
                        if (id.isEmpty() || isMainUiUrl(url)) {
                            continue;
                        }
                        if (!TextUtils.isEmpty(markerUrl) && url.contains(markerUrl)) {
                            return id;
                        }
                        synchronized (tabLock) {
                            if (!tabsByCdpId.containsKey(id) && !beforeIds.contains(id)) {
                                fallbackCandidate = id;
                            }
                        }
                    }
                    if (fallbackCandidate != null) {
                        return fallbackCandidate;
                    }
                }
            } catch (Exception ignored) {
            }
            try {
                Thread.sleep(TARGET_POLL_INTERVAL_MS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
        return null;
    }

    private AgentTab getSelectedTab() {
        synchronized (tabLock) {
            String id = getSelectedTargetId();
            return id != null ? tabsByCdpId.get(id) : null;
        }
    }

    private AgentTab resolveTab(String targetId) {
        synchronized (tabLock) {
            if (!TextUtils.isEmpty(targetId) && tabsByCdpId.containsKey(targetId)) {
                return tabsByCdpId.get(targetId);
            }
            String id = getSelectedTargetId();
            return id != null ? tabsByCdpId.get(id) : null;
        }
    }

    private String normalizeRequestedUrl(String url) {
        if (TextUtils.isEmpty(url)) {
            return "about:blank";
        }
        String trimmed = url.trim();
        if (trimmed.isEmpty()) {
            return "about:blank";
        }
        if (trimmed.startsWith("http://") || trimmed.startsWith("https://")
                || trimmed.startsWith("about:") || trimmed.startsWith("data:")
                || trimmed.startsWith("file://")) {
            return trimmed;
        }
        return "https://" + trimmed;
    }

    @SuppressLint({"SetJavaScriptEnabled", "ClickableViewAccessibility"})
    private void ensureViewsCreated(Activity activity) {
        if (overlayContext == null) {
            overlayContext = new MutableContextWrapper(activity != null ? activity : appContext);
        } else if (activity != null) {
            overlayContext.setBaseContext(activity);
        }

        if (agentContainer == null) {
            agentContainer = new FrameLayout(overlayContext);
            agentContainer.setBackgroundColor(Color.WHITE);

            LinearLayout shell = new LinearLayout(overlayContext);
            shell.setOrientation(LinearLayout.VERTICAL);

            toolbarView = buildToolbar(overlayContext);
            toolbarView.setVisibility(View.GONE);
            shell.addView(toolbarView, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(44)));

            webViewStage = new FrameLayout(overlayContext);
            webViewStage.setBackgroundColor(Color.WHITE);
            shell.addView(webViewStage, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

            agentContainer.addView(shell, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }

        if (floatingPill == null) {
            floatingPill = new TextView(overlayContext);
            floatingPill.setText("Agent Browser");
            floatingPill.setTextColor(Color.WHITE);
            floatingPill.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
            floatingPill.setTypeface(Typeface.DEFAULT_BOLD);
            floatingPill.setPadding(dp(12), dp(7), dp(12), dp(7));
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(0xE61F2937);
            bg.setCornerRadius(dp(18));
            bg.setStroke(dp(1), 0x664B5563);
            floatingPill.setBackground(bg);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                floatingPill.setElevation(dp(8));
            }
            floatingPill.setVisibility(View.GONE);
            floatingPill.setOnClickListener(v -> expand());

            // Allow dragging the compact pill vertically along the right edge so it never traps UI behind it
            floatingPill.setOnTouchListener(new View.OnTouchListener() {
                float startY;
                float startTranslationY;
                boolean dragging = false;

                @Override
                public boolean onTouch(View v, MotionEvent event) {
                    switch (event.getActionMasked()) {
                        case MotionEvent.ACTION_DOWN:
                            startY = event.getRawY();
                            startTranslationY = v.getTranslationY();
                            dragging = false;
                            return true;
                        case MotionEvent.ACTION_MOVE:
                            float dy = event.getRawY() - startY;
                            if (Math.abs(dy) > dp(6)) {
                                dragging = true;
                            }
                            if (dragging) {
                                v.setTranslationY(startTranslationY + dy);
                            }
                            return true;
                        case MotionEvent.ACTION_UP:
                            if (!dragging) {
                                v.performClick();
                            }
                            return true;
                        default:
                            return false;
                    }
                }
            });
        }
    }

    private LinearLayout buildToolbar(Context ctx) {
        LinearLayout bar = new LinearLayout(ctx);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(0xFF111827);
        bar.setPadding(dp(8), 0, dp(8), 0);

        TextView backBtn = makeToolbarButton(ctx, "<", v -> {
            AgentTab t = getSelectedTab();
            if (t != null && t.webView != null && t.webView.canGoBack()) {
                t.webView.goBack();
            }
        });
        bar.addView(backBtn);

        TextView reloadBtn = makeToolbarButton(ctx, "↻", v -> {
            AgentTab t = getSelectedTab();
            if (t != null && t.webView != null) {
                t.webView.reload();
            }
        });
        bar.addView(reloadBtn);

        urlTitleView = new TextView(ctx);
        urlTitleView.setTextColor(0xFFE5E7EB);
        urlTitleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        urlTitleView.setSingleLine(true);
        urlTitleView.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        urlTitleView.setPadding(dp(8), 0, dp(8), 0);
        LinearLayout.LayoutParams urlLp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        bar.addView(urlTitleView, urlLp);

        tabBadgeView = new TextView(ctx);
        tabBadgeView.setTextColor(0xFF93C5FD);
        tabBadgeView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        tabBadgeView.setTypeface(Typeface.DEFAULT_BOLD);
        tabBadgeView.setPadding(dp(8), dp(4), dp(8), dp(4));
        tabBadgeView.setOnClickListener(v -> cycleNextTab());
        bar.addView(tabBadgeView);

        TextView hideBtn = makeToolbarButton(ctx, "Hide", v -> collapse());
        bar.addView(hideBtn);

        return bar;
    }

    private TextView makeToolbarButton(Context ctx, String label, View.OnClickListener listener) {
        TextView btn = new TextView(ctx);
        btn.setText(label);
        btn.setTextColor(Color.WHITE);
        btn.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        btn.setTypeface(Typeface.DEFAULT_BOLD);
        btn.setPadding(dp(10), dp(6), dp(10), dp(6));
        btn.setOnClickListener(listener);
        return btn;
    }

    private void cycleNextTab() {
        synchronized (tabLock) {
            if (tabsByCdpId.size() <= 1) {
                return;
            }
            List<String> ids = new ArrayList<>(tabsByCdpId.keySet());
            int idx = selectedTargetId != null ? ids.indexOf(selectedTargetId) : -1;
            selectedTargetId = ids.get((idx + 1) % ids.size());
        }
        syncOverlayStateOnMain();
    }

    private void mountViewsIntoHost(FrameLayout hostRoot) {
        if (hostRoot == null || agentContainer == null || floatingPill == null) {
            return;
        }
        if (agentContainer.getParent() != hostRoot) {
            detachFromParent(agentContainer);
            FrameLayout.LayoutParams full = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
            int idx = expanded ? hostRoot.getChildCount() : 0;
            hostRoot.addView(agentContainer, Math.min(idx, hostRoot.getChildCount()), full);
        }
        if (floatingPill.getParent() != hostRoot) {
            detachFromParent(floatingPill);
            FrameLayout.LayoutParams pillLp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            pillLp.gravity = Gravity.TOP | Gravity.END;
            pillLp.topMargin = dp(56);
            pillLp.rightMargin = dp(12);
            hostRoot.addView(floatingPill, pillLp);
        }
    }

    private void syncOverlayStateOnMain() {
        FrameLayout hostRoot = hostRootRef.get();
        int count = getTabCount();
        AgentTab wcvTab = wcvVisible ? resolveTabByKey(activeWcvTabKey) : null;
        AgentTab selected = wcvTab != null ? wcvTab : getSelectedTab();

        if (agentContainer != null && webViewStage != null) {
            synchronized (tabLock) {
                for (AgentTab tab : tabsByCdpId.values()) {
                    if (tab.webView == null) {
                        continue;
                    }
                    if (tab.webView.getParent() != webViewStage) {
                        detachFromParent(tab.webView);
                        webViewStage.addView(tab.webView, new FrameLayout.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.MATCH_PARENT));
                    }
                    tab.webView.setVisibility(tab == selected ? View.VISIBLE : View.INVISIBLE);
                }
            }

            if (expanded && count > 0) {
                agentContainer.setDescendantFocusability(ViewGroup.FOCUS_AFTER_DESCENDANTS);
                if (toolbarView != null) {
                    toolbarView.setVisibility(View.VISIBLE);
                }
                if (hostRoot != null) {
                    agentContainer.setLayoutParams(new FrameLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.MATCH_PARENT));
                    agentContainer.bringToFront();
                    if (floatingPill != null) {
                        floatingPill.setVisibility(View.GONE);
                    }
                }
            } else if (wcvVisible && !wcvOverlayActive && wcvTab != null
                    && wcvWidthPx > 2 && wcvHeightPx > 2 && hostRoot != null) {
                // Docked directly inside Antigravity's Side Panel browser viewport rectangle
                agentContainer.setDescendantFocusability(ViewGroup.FOCUS_AFTER_DESCENDANTS);
                if (toolbarView != null) {
                    toolbarView.setVisibility(View.GONE);
                }
                FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(wcvWidthPx, wcvHeightPx);
                lp.leftMargin = wcvLeftPx;
                lp.topMargin = wcvTopPx;
                agentContainer.setLayoutParams(lp);
                agentContainer.bringToFront();
            } else {
                // Keep VISIBLE at index 0 behind opaque main UI WebView so Chromium keeps painting frames,
                // while blocking focus so agent pages cannot steal keyboard input from main chat.
                agentContainer.setDescendantFocusability(ViewGroup.FOCUS_BLOCK_DESCENDANTS);
                if (toolbarView != null) {
                    toolbarView.setVisibility(View.GONE);
                }
                if (hostRoot != null) {
                    FrameLayout.LayoutParams full = new FrameLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.MATCH_PARENT);
                    if (hostRoot.indexOfChild(agentContainer) > 0) {
                        hostRoot.removeView(agentContainer);
                        hostRoot.addView(agentContainer, 0, full);
                    } else {
                        agentContainer.setLayoutParams(full);
                    }
                }
            }
        }

        if (toolbarView != null && selected != null) {
            String display = !TextUtils.isEmpty(selected.currentUrl)
                    ? selected.currentUrl : selected.currentTitle;
            urlTitleView.setText(display != null ? display : "about:blank");
            tabBadgeView.setText(count > 1 ? ("Tabs (" + count + ")") : "1 tab");
        }

        if (floatingPill != null) {
            // Hide the fallback floating pill when the Side Panel web host is attached
            boolean showFallbackPill = count > 0 && !expanded && !wcvVisible
                    && hostRoot != null && uiWebViewRef.get() == null;
            if (showFallbackPill) {
                floatingPill.setText(count == 1 ? "Agent Browser" : ("Agent Browser (" + count + ")"));
                floatingPill.setVisibility(View.VISIBLE);
                floatingPill.bringToFront();
            } else {
                floatingPill.setVisibility(View.GONE);
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private AgentTab createTabViewOnMain(String token, int webContentsId, WebView existingWebView) {
        ensureViewsCreated(activityRef.get());
        WebView wv = existingWebView != null ? existingWebView : new WebView(overlayContext);
        configureAgentWebSettings(wv);

        AgentTab tab = new AgentTab(token, webContentsId, wv);
        wv.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                tab.loading = true;
                if (!isInternalMarkerUrl(url)) {
                    tab.currentUrl = url != null ? url : "about:blank";
                    updateToolbarIfSelected(tab);
                    emitWcvHostEvent("did-start-loading", tab, null);
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                tab.loading = false;
                if (!isInternalMarkerUrl(url)) {
                    tab.currentUrl = url != null ? url : "about:blank";
                    String title = view.getTitle();
                    if (title != null) {
                        tab.currentTitle = title;
                    }
                    updateToolbarIfSelected(tab);
                    emitWcvHostEvent("did-navigate", tab, null);
                    if (!tab.domReady) {
                        tab.domReady = true;
                        emitWcvHostEvent("dom-ready", tab, null);
                    }
                    emitWcvHostEvent("did-stop-loading", tab, null);
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return false;
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                Log.w(TAG, "Agent WebView render process gone for tab " + tab.token);
                if (tab.cdpTargetId != null) {
                    closeTab(tab.cdpTargetId);
                } else {
                    destroyTabViewOnMain(tab);
                }
                return true;
            }
        });

        wv.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onReceivedTitle(WebView view, String title) {
                if (title != null && !isInternalMarkerUrl(view.getUrl())) {
                    tab.currentTitle = title;
                    updateToolbarIfSelected(tab);
                    emitWcvHostEvent("page-title-updated", tab, null);
                }
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
                if (resultMsg == null || !(resultMsg.obj instanceof WebView.WebViewTransport)) {
                    return false;
                }
                int childSeq = tokenSeq.incrementAndGet();
                String childToken = "agy-" + childSeq;
                Set<String> knownBefore = Collections.emptySet();
                AgentTab child = createTabViewOnMain(childToken, childSeq, null);
                WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMsg.obj;
                transport.setWebView(child.webView);
                resultMsg.sendToTarget();

                bgExecutor.execute(() -> {
                    String cdpId = awaitNewCdpTargetId(knownBefore, null);
                    if (!TextUtils.isEmpty(cdpId)) {
                        child.cdpTargetId = cdpId;
                        child.tabKey = "browser__cdp_" + cdpId;
                        synchronized (tabLock) {
                            tabsByCdpId.put(cdpId, child);
                            tabsByKey.put(child.tabKey, child);
                            selectedTargetId = cdpId;
                        }
                        runOnMain(() -> {
                            syncOverlayStateOnMain();
                            emitWcvHostEvent("agent-tab-created", child, null);
                        });
                    }
                });
                return true;
            }

            @Override
            public void onCloseWindow(WebView window) {
                if (tab.cdpTargetId != null) {
                    closeTab(tab.cdpTargetId);
                }
            }

            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                replacePendingDialog(tab, new PendingDialog("alert", message, null, result));
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                replacePendingDialog(tab, new PendingDialog("confirm", message, null, result));
                return true;
            }

            @Override
            public boolean onJsPrompt(WebView view, String url, String message, String defaultValue, JsPromptResult result) {
                replacePendingDialog(tab, new PendingDialog("prompt", message, defaultValue, result));
                return true;
            }
        });

        if (webViewStage != null) {
            webViewStage.addView(wv, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        return tab;
    }

    private void replacePendingDialog(AgentTab tab, PendingDialog next) {
        PendingDialog prev = tab.pendingDialog;
        tab.pendingDialog = next;
        if (prev != null && prev.result != null) {
            try {
                prev.result.cancel();
            } catch (Exception ignored) {
            }
        }
    }

    private boolean isInternalMarkerUrl(String url) {
        return url != null && url.contains(MARKER_QUERY_KEY);
    }

    private void updateToolbarIfSelected(AgentTab tab) {
        runOnMain(() -> {
            if (tab == getSelectedTab() && urlTitleView != null && expanded) {
                String label = !TextUtils.isEmpty(tab.currentUrl) ? tab.currentUrl : tab.currentTitle;
                urlTitleView.setText(label != null ? label : "about:blank");
            }
        });
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureAgentWebSettings(WebView view) {
        WebSettings s = view.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setSupportMultipleWindows(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
            CookieManager.getInstance().setAcceptThirdPartyCookies(view, true);
        }
        CookieManager.getInstance().setAcceptCookie(true);

        // Omit embedded "; wv" token so sites treat this as a standard mobile browser context.
        String ua = s.getUserAgentString();
        if (ua != null && ua.contains("; wv)")) {
            s.setUserAgentString(ua.replace("; wv)", ")"));
        }
    }

    private void destroyTabViewOnMain(AgentTab tab) {
        if (tab == null) {
            return;
        }
        PendingDialog dlg = tab.pendingDialog;
        tab.pendingDialog = null;
        if (dlg != null && dlg.result != null) {
            try {
                dlg.result.cancel();
            } catch (Exception ignored) {
            }
        }
        WebView wv = tab.webView;
        tab.webView = null;
        if (wv != null) {
            try {
                wv.stopLoading();
                wv.loadUrl("about:blank");
                detachFromParent(wv);
                wv.removeAllViews();
                wv.destroy();
            } catch (Exception e) {
                Log.w(TAG, "Error destroying agent tab WebView", e);
            }
        }
    }

    private static void detachFromParent(View v) {
        if (v == null) {
            return;
        }
        ViewParent parent = v.getParent();
        if (parent instanceof ViewGroup) {
            ((ViewGroup) parent).removeView(v);
        }
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, value, appContext.getResources().getDisplayMetrics()));
    }

    private void runOnMain(Runnable task) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            task.run();
        } else {
            mainHandler.post(task);
        }
    }

    private void runOnMainSync(Runnable task) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            task.run();
            return;
        }
        CountDownLatch latch = new CountDownLatch(1);
        mainHandler.post(() -> {
            try {
                task.run();
            } finally {
                latch.countDown();
            }
        });
        try {
            latch.await(5, TimeUnit.SECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
        }
    }
}
