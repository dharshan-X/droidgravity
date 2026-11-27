#!/usr/bin/env python3
"""
web/override_html.py - Non-destructively injects Android mobile UI override
scripts into upstream index.html without overwriting upstream markup/styles.
"""
import os
import re
import sys

HEAD_BOOT_SNIPPET = (
    '    <script src="./generated/boot/custom-elements-guard.js"></script>\n'
    '    <script src="./generated/boot/theme-bootstrap.js"></script>\n'
)

BODY_PATCH_SNIPPET = (
    '    <script src="./generated/patches/web_optimizations.js"></script>\n'
    '    <script src="./generated/patches/touch_model_menu.js"></script>\n'
    '    <script src="./generated/patches/theme_sync.js"></script>\n'
    '    <script src="./generated/patches/side_panel_browser.js"></script>\n'
    '    <script src="./generated/bridge.core.js"></script>\n'
)

MARKER = "./generated/bridge.core.js"


def override_html(html_path: str) -> bool:
    if not os.path.isfile(html_path):
        print(f"[web-html] file not found: {html_path}", file=sys.stderr)
        return False

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    if MARKER in html:
        print(f"[web-html] mobile overrides already present in {html_path}")
        return True

    # 1. Inject boot scripts right after <head> (after <meta charset...> if present)
    head_match = re.search(r"<head[^>]*>\s*(?:<meta[^>]*charset[^>]*>\s*)?", html, re.IGNORECASE)
    if not head_match:
        print("[web-html] could not locate <head> in index.html", file=sys.stderr)
        return False
    insert_head = head_match.end()
    html = html[:insert_head] + "\n" + HEAD_BOOT_SNIPPET + html[insert_head:]

    # 2. Inject mobile patches + bridge.core.js immediately before upstream main.js
    main_script_match = re.search(
        r'<script\b[^>]*\bsrc=["\'](?:\.?/)?(?:generated/)?main\.js["\'][^>]*>\s*</script>',
        html,
        re.IGNORECASE,
    )
    if main_script_match:
        at = main_script_match.start()
        html = html[:at] + BODY_PATCH_SNIPPET + html[at:]
    else:
        body_end = re.search(r"</body>", html, re.IGNORECASE)
        if not body_end:
            print("[web-html] could not locate main.js script or </body>", file=sys.stderr)
            return False
        at = body_end.start()
        html = html[:at] + BODY_PATCH_SNIPPET + html[at:]

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[web-html] Injected mobile UI overrides into upstream {html_path}")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <index.html>", file=sys.stderr)
        return 2
    return 0 if override_html(sys.argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main())
