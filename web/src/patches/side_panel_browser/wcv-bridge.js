/* Android Side Panel WebContentsView (wcvBrowser) bridge for Antigravity. */
(function () {
  if (typeof window === "undefined" || window.__agWcvBrowser) {
    return;
  }

  var guestListeners = new Set();
  var zOrderListeners = new Set();
  var cursorListeners = new Set();
  var shortcutListeners = new Set();
  var pendingOpenQueue = [];
  var recentBlankCdpTabs = new Map();

  function getToken() {
    return (
      window.__AG_NATIVE_BRIDGE_TOKEN__ ||
      (window.__APP_CONFIG__ && window.__APP_CONFIG__.blobBridgeToken) ||
      ""
    );
  }

  function hasNative() {
    return !!(window.Android && typeof window.Android.wcvAcquire === "function");
  }

  function mapBounds(boundsCss) {
    var b = boundsCss || {};
    var x = typeof b.x === "number" ? b.x : (b.left || 0);
    var y = typeof b.y === "number" ? b.y : (b.top || 0);
    var vw = window.innerWidth || document.documentElement.clientWidth || 0;
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    var dpr = window.devicePixelRatio || 1;
    return {
      x: Number(x) || 0,
      y: Number(y) || 0,
      w: Math.max(0, Number(b.width) || 0),
      h: Math.max(0, Number(b.height) || 0),
      vw: Number(vw) || 0,
      vh: Number(vh) || 0,
      dpr: Number(dpr) || 1
    };
  }

  function safeParse(jsonStr, fallback) {
    if (!jsonStr) return fallback;
    try {
      return JSON.parse(jsonStr);
    } catch (_) {
      return fallback;
    }
  }

  function flushPendingTabs() {
    if (typeof window.__agOpenSidePanelBrowserTab !== "function") {
      return;
    }
    while (pendingOpenQueue.length > 0) {
      var item = pendingOpenQueue.shift();
      try {
        window.__agOpenSidePanelBrowserTab(item.tabKey, item.url);
      } catch (_) {}
    }
  }

  window.__agWcvBrowser = {
    acquire: function (opts) {
      if (!hasNative()) return Promise.resolve({ webContentsId: 0 });
      var o = opts || {};
      var res = window.Android.wcvAcquire(getToken(), String(o.tabKey || ""), String(o.initialUrl || ""));
      return Promise.resolve(safeParse(res, { webContentsId: 0 }));
    },
    show: function (tabKey, boundsCss) {
      if (!hasNative()) return Promise.resolve({ webContentsId: 0 });
      var s = mapBounds(boundsCss);
      var res = window.Android.wcvShow(
        getToken(),
        String(tabKey || ""),
        Math.round(s.x * 100),
        Math.round(s.y * 100),
        Math.round(s.w * 100),
        Math.round(s.h * 100),
        Math.round(s.vw * 100),
        Math.round(s.vh * 100)
      );
      return Promise.resolve(safeParse(res, { webContentsId: 0 }));
    },
    setBounds: function (tabKey, boundsCss) {
      if (!hasNative()) return;
      var s = mapBounds(boundsCss);
      window.Android.wcvSetBounds(
        getToken(),
        String(tabKey || ""),
        Math.round(s.x * 100),
        Math.round(s.y * 100),
        Math.round(s.w * 100),
        Math.round(s.h * 100),
        Math.round(s.vw * 100),
        Math.round(s.vh * 100)
      );
    },
    hide: function (tabKey) {
      if (!hasNative()) return;
      window.Android.wcvHide(getToken(), String(tabKey || ""));
    },
    setOverlayActive: function (tabKey, active) {
      if (!hasNative()) return Promise.resolve(null);
      window.Android.wcvSetOverlayActive(getToken(), String(tabKey || ""), !!active);
      return Promise.resolve(null);
    },
    elevate: function () {
      return Promise.resolve({ zOrder: 1 });
    },
    demote: function () {
      return Promise.resolve({ zOrder: 0 });
    },
    navigate: function (tabKey, url) {
      if (!hasNative()) return Promise.resolve();
      var k = String(tabKey || "");
      var u = String(url || "");
      var cdpIdx = k.indexOf("browser__cdp_");
      var rawCdpKey = cdpIdx >= 0 ? k.slice(cdpIdx) : k;
      var createdAt = recentBlankCdpTabs.get(rawCdpKey);
      if (createdAt && u === "https://www.google.com/" && Date.now() - createdAt < 2000) {
        recentBlankCdpTabs.delete(rawCdpKey);
        return Promise.resolve();
      }
      window.Android.wcvNavigate(getToken(), k, u);
      return Promise.resolve();
    },
    control: function (tabKey, action) {
      if (!hasNative()) return;
      window.Android.wcvControl(getToken(), String(tabKey || ""), String(action || ""));
    },
    setAgentControlling: function (_tabKey, controlling) {
      return Promise.resolve({ isAgentStepActive: !!controlling });
    },
    setPinned: function () {},
    setDragOver: function () {
      return Promise.resolve(null);
    },
    sendMouse: function () {
      return Promise.resolve();
    },
    insertText: function () {
      return Promise.resolve();
    },
    setViewportSize: function () {
      return Promise.resolve(false);
    },
    capturePage: function () {
      return Promise.resolve(null);
    },
    dispose: function (tabKey) {
      if (!hasNative()) return;
      window.Android.wcvDispose(getToken(), String(tabKey || ""));
    },
    onGuestEvent: function (cb) {
      if (typeof cb === "function") guestListeners.add(cb);
      return function () { guestListeners.delete(cb); };
    },
    onZOrderChanged: function (cb) {
      if (typeof cb === "function") zOrderListeners.add(cb);
      return function () { zOrderListeners.delete(cb); };
    },
    onCursorChanged: function (cb) {
      if (typeof cb === "function") cursorListeners.add(cb);
      return function () { cursorListeners.delete(cb); };
    },
    onShortcutForwarded: function (cb) {
      if (typeof cb === "function") shortcutListeners.add(cb);
      return function () { shortcutListeners.delete(cb); };
    }
  };

  window.__agWcvHost = {
    registerSidePanelOpener: function (fn) {
      if (typeof fn === "function") {
        window.__agOpenSidePanelBrowserTab = fn;
        flushPendingTabs();
      }
    },
    onEvent: function (ev) {
      if (!ev || !ev.type) return;
      if (ev.type === "agent-tab-created") {
        var u = ev.url || "about:blank";
        if (ev.tabKey && (!ev.url || ev.url === "about:blank")) {
          recentBlankCdpTabs.set(String(ev.tabKey), Date.now());
        }
        if (typeof window.__agOpenSidePanelBrowserTab === "function") {
          try {
            window.__agOpenSidePanelBrowserTab(ev.tabKey, u);
          } catch (_) {}
        } else {
          pendingOpenQueue.push({ tabKey: ev.tabKey, url: u });
        }
        return;
      }
      if (ev.type === "agent-tab-closed") {
        return;
      }
      guestListeners.forEach(function (cb) {
        try {
          cb(ev);
        } catch (_) {}
      });
    }
  };
})();
