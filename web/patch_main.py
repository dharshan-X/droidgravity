#!/usr/bin/env python3
"""
web/patch_main.py - Dynamic patcher for web/main.js.

Locates the Appearance settings block and injects the Desktop Mode toggle
if not already present. Safe, idempotent, and resilient to upstream changes.
"""

import sys
import os
import re

def patch_folder_picker_initial_path(content):
    if "externalStorageDir" in content:
        print("[web-patch] Folder picker initialPath already patched in main.js")
        return content, True

    pattern = re.compile(r'(initialPath:)([^,]+)(,fetchDirectoryContents:)')
    m = pattern.search(content)
    if not m:
        print("[web-patch] Interactive folder picker initialPath not matched in main.js", file=sys.stderr)
        return content, False

    orig_expr = m.group(2)
    new_expr = (
        '(window.__APP_CONFIG__?.externalStorageDir||'
        '(typeof window!=="undefined"&&window.Android?"/storage/emulated/0":'
        f'({orig_expr})))'
    )
    content = content[:m.start(2)] + new_expr + content[m.end(2):]
    print("[web-patch] Successfully patched folder picker initialPath into main.js")
    return content, True


def patch_main(file_path):
    if not os.path.isfile(file_path):
        print(f"[web-patch] file not found: {file_path}", file=sys.stderr)
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False

    if "Desktop Mode" in content or "ag_desktop_mode" in content:
        print("[web-patch] Desktop Mode toggle already present in main.js")
    else:
        # 1. Locate Theme element in Appearance settings
        theme_idx = content.find('{label:"Theme"')
        theme_match = None
        react_id = "A"
        if theme_idx != -1:
            window_start = max(0, theme_idx - 100)
            window_end = min(len(content), theme_idx + 500)
            window_text = content[window_start:window_end]
            theme_pattern = re.compile(
                r'([A-Za-z0-9_$]+)\.createElement\(([A-Za-z0-9_$]+),\{label:"Theme",.*?onSelect:[A-Za-z0-9_$]+\}\)'
            )
            theme_match = theme_pattern.search(window_text)
            if theme_match:
                react_id = theme_match.group(1)

        if not theme_match:
            print("[web-patch] Theme element not matched in main.js", file=sys.stderr)
            return False

        # 2. Locate hook injection site before the Appearance return statement
        hook_region_start = max(0, theme_idx - 500)
        hook_region = content[hook_region_start:theme_idx]
        ret_pattern = re.compile(
            r'return\s+' + re.escape(react_id) + r'\.createElement\(' + re.escape(react_id) + r'\.Fragment,null,' + re.escape(react_id) + r'\.createElement\([A-Za-z0-9_$]+,\{title:"Appearance"\}'
        )
        ret_match = ret_pattern.search(hook_region)
        if not ret_match:
            print("[web-patch] Appearance return statement not matched in main.js", file=sys.stderr)
            return False

        ret_abs_pos = hook_region_start + ret_match.start()

        injected_hook = (
            f"var [isDesktop,setIsDesktop]=(0,{react_id}.useState)(()=>{{"
            "try{if(window.Android&&typeof window.Android.isDesktopMode==='function')return window.Android.isDesktopMode();"
            "var s=localStorage.getItem('ag_desktop_mode');return s===null?!1:s==='true'}catch(_){return !1}});"
            f"var onDesktopToggle=(0,{react_id}.useCallback)(v=>{{"
            "setIsDesktop(v);try{localStorage.setItem('ag_desktop_mode',String(v));"
            "if(window.Android&&typeof window.Android.setDesktopMode==='function')window.Android.setDesktopMode(v)}catch(_){}},[]);"
        )

        # 3. Detect setting item and switch components from any rightElement switch row
        item_comp = None
        switch_comp = None
        for m in re.finditer(r'rightElement:', content):
            pos = m.start()
            re_window = content[max(0, pos - 220):min(len(content), pos + 220)]
            item_m = re.search(
                r'([A-Za-z0-9_$]+)\.createElement\(([A-Za-z0-9_$]+),\{label:[^}]*?rightElement:',
                re_window,
            )
            sw_m = re.search(
                r'rightElement:[^}]*?([A-Za-z0-9_$]+)\.createElement\(([A-Za-z0-9_$]+),\{checked:',
                re_window,
            )
            if item_m and sw_m:
                item_comp = item_m.group(2)
                switch_comp = sw_m.group(2)
                break

        if not item_comp or not switch_comp:
            item_comp = item_comp or "v3"
            switch_comp = switch_comp or "dX"

        orig_theme = theme_match.group(0)
        injected_theme = (
            orig_theme
            + f',{react_id}.createElement({item_comp},{{label:"Desktop Mode",description:"Use desktop viewport and user agent for the workspace",'
            + f'rightElement:{react_id}.createElement({switch_comp},{{checked:isDesktop,onCheckedChange:onDesktopToggle}})}})'
        )

        # Inject hook before return (ret_abs_pos < theme_idx), then replace theme element
        content = content[:ret_abs_pos] + injected_hook + content[ret_abs_pos:]
        content = content.replace(orig_theme, injected_theme, 1)
        changed = True
        print(f"[web-patch] Successfully patched Desktop Mode toggle into main.js (item={item_comp}, switch={switch_comp})")

    content, picker_ok = patch_folder_picker_initial_path(content)
    if not picker_ok:
        return False

    content, sp_changed = patch_side_panel_browser(content)
    changed = changed or sp_changed

    if changed or "externalStorageDir" in content:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

    return True


def patch_side_panel_browser(content):
    if "__agWcvBrowser" in content:
        print("[web-patch] Side Panel browser already patched in main.js")
        return content, False

    did_change = False

    # 1. Hook Gpb() so __agWcvBrowser is recognized alongside electronNative.wcvBrowser
    gpb_old = "a?.electronNative?.wcvBrowser)&&typeof a.acquire===\"function\""
    gpb_new = "a?.electronNative?.wcvBrowser||a?.__agWcvBrowser)&&typeof a.acquire===\"function\""
    if gpb_old in content:
        content = content.replace(gpb_old, gpb_new, 1)
        did_change = True
        print("[web-patch] Hooked __agWcvBrowser into Gpb() in main.js")
    else:
        print("[web-patch] Gpb() pattern not matched in main.js", file=sys.stderr)

    # 2. Enable browserAuxPane, browserSubagentWebview, and browserSubagentWebviewUx when window.Android is present
    lob_m = re.search(
        r'customFlag:"enable-hub-builtin-browser"\},feature:[A-Za-z0-9_$]+\?([A-Za-z0-9_$]+):!1',
        content,
    )
    if lob_m:
        lob_id = lob_m.group(1)
        bap_old = "browserAuxPane:{feature:!1,"
        bap_new = f'browserAuxPane:{{feature:(typeof window!=="undefined"&&window.Android?{lob_id}:!1),'
        if bap_old in content:
            content = content.replace(bap_old, bap_new, 1)
            did_change = True
            print(f"[web-patch] Enabled browserAuxPane for Android (pane={lob_id})")
    else:
        print("[web-patch] browserAuxPane LOb identifier not matched in main.js", file=sys.stderr)

    for flag_key in ("browserSubagentWebview", "browserSubagentWebviewUx"):
        old_s = f"{flag_key}:{{feature:!1,"
        new_s = f'{flag_key}:{{feature:(typeof window!=="undefined"&&window.Android?!0:!1),'
        if old_s in content:
            content = content.replace(old_s, new_s, 1)
            did_change = True
            print(f"[web-patch] Enabled {flag_key} for Android")

    # 3. Register Side Panel browser opener hook in ahd() so CDP agent tabs open inside the Side Panel
    opener_m = re.search(
        r'([A-Za-z0-9_$]+)=xU\(\);(\(0,([A-Za-z0-9_$]+)\.useEffect\)\(\(\)=>fZa\()',
        content,
    )
    if opener_m:
        open_fn = opener_m.group(1)
        react_var = opener_m.group(3)
        injected_effect = (
            f"{open_fn}=xU();"
            f"(0,{react_var}.useEffect)(()=>{{window.__agWcvHost?.registerSidePanelOpener?.((tid,u)=>{{try{{"
            f"{open_fn}({{type:\"customView\",customView:{{customPaneKey:\"browser\",serializedInputs:JSON.stringify({{tabId:tid,initialUrl:u||\"about:blank\"}})}}}},tid,{{keepOpen:!0}})"
            f"}}catch(_){{}}}})}},[{open_fn}]);"
            f"{opener_m.group(2)}"
        )
        content = content[:opener_m.start()] + injected_effect + content[opener_m.end():]
        did_change = True
        print(f"[web-patch] Registered Side Panel browser opener in main.js (fn={open_fn})")
    else:
        print("[web-patch] Side Panel opener hook not matched in main.js", file=sys.stderr)

    return content, did_change

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "main.js")
    if not patch_main(path):
        sys.exit(1)
