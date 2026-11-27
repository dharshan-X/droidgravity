#!/usr/bin/env python3
"""Unit and integration tests for modules/core/shims/browser/chrome_devtools_mcp.py."""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SHIM_PATH = os.path.join(REPO_ROOT, "modules", "core", "shims", "browser", "chrome_devtools_mcp.py")
sys.path.insert(0, os.path.dirname(SHIM_PATH))

import chrome_devtools_mcp as mcp  # noqa: E402


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MockCdpServer:
    """In-process HTTP + RFC 6455 WebSocket mock simulating Android WebView CDP proxy."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]

        self.lock = threading.Lock()
        self.running = True
        self.seq = 1
        self.targets = {
            "TARGET_1": {
                "id": "TARGET_1",
                "type": "page",
                "title": "Initial Tab",
                "url": "https://example.org/start",
            }
        }
        self.activated_ids = []
        self.closed_ids = []
        self.mouse_events = []
        self.key_events = []
        self.inserted_texts = []
        self.emulated_sizes = []
        self.dialog_handled = []
        self.body_text = "Welcome to Droidgravity Agent Browser"
        self.ws_sockets = []

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
        for s in list(self.ws_sockets):
            try:
                s.close()
            except OSError:
                pass

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
                conn.settimeout(5.0)
                t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
                t.start()
            except OSError:
                break

    def _handle_conn(self, conn: socket.socket):
        try:
            raw = bytearray()
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                raw.extend(chunk)
            hdr_end = raw.index(b"\r\n\r\n")
            header_text = raw[:hdr_end].decode("ascii", errors="replace")
            tail = bytes(raw[hdr_end + 4 :])
            lines = header_text.split("\r\n")
            req_line = lines[0].split(" ")
            method, target = req_line[0], req_line[1]
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if headers.get("upgrade", "").lower() == "websocket":
                conn.settimeout(None)
                self._serve_ws(conn, target, headers, tail)
                return

            parsed = urllib.parse.urlparse(target)
            path = parsed.path
            query = parsed.query

            if path == "/json/version":
                body = json.dumps({
                    "Browser": "Android WebView/130.0",
                    "Protocol-Version": "1.3",
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser",
                })
                self._send_http(conn, 200, "application/json", body)
            elif path in ("/json", "/json/list"):
                with self.lock:
                    arr = []
                    for t in self.targets.values():
                        item = dict(t)
                        item["webSocketDebuggerUrl"] = f"ws://127.0.0.1:{self.port}/devtools/page/{t['id']}"
                        arr.append(item)
                self._send_http(conn, 200, "application/json", json.dumps(arr))
            elif path == "/json/new":
                url = "about:blank"
                if query.startswith("url="):
                    url = urllib.parse.unquote(query[4:].split("&", 1)[0])
                elif query:
                    url = urllib.parse.unquote(query)
                with self.lock:
                    self.seq += 1
                    tid = f"TARGET_{self.seq}"
                    entry = {"id": tid, "type": "page", "title": "", "url": url}
                    self.targets[tid] = entry
                    resp = dict(entry)
                    resp["webSocketDebuggerUrl"] = f"ws://127.0.0.1:{self.port}/devtools/page/{tid}"
                self._send_http(conn, 200, "application/json", json.dumps(resp))
            elif path.startswith("/json/activate/"):
                tid = path.rsplit("/", 1)[-1]
                with self.lock:
                    self.activated_ids.append(tid)
                self._send_http(conn, 200, "text/plain", "Target activated")
            elif path.startswith("/json/close/"):
                tid = path.rsplit("/", 1)[-1]
                with self.lock:
                    self.closed_ids.append(tid)
                    self.targets.pop(tid, None)
                self._send_http(conn, 200, "text/plain", "Target is closing")
            else:
                self._send_http(conn, 404, "text/plain", "Not Found")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send_http(self, conn: socket.socket, status: int, ctype: str, body: str):
        data = body.encode("utf-8")
        hdr = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: {ctype}; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        conn.sendall(hdr + data)

    def _serve_ws(self, conn: socket.socket, target: str, headers: dict, tail: bytes):
        self.ws_sockets.append(conn)
        tid = target.rsplit("/", 1)[-1]
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

        buf = bytearray(tail)
        write_lock = threading.Lock()

        def recv_exact(n: int) -> bytes:
            while len(buf) < n:
                c = conn.recv(max(4096, n - len(buf)))
                if not c:
                    raise EOFError()
                buf.extend(c)
            out = bytes(buf[:n])
            del buf[:n]
            return out

        def send_text(obj: dict):
            payload = json.dumps(obj).encode("utf-8")
            frame = bytearray([0x81])
            ln = len(payload)
            if ln < 126:
                frame.append(ln)
            elif ln <= 0xFFFF:
                frame.append(126)
                frame.extend(struct.pack("!H", ln))
            else:
                frame.append(127)
                frame.extend(struct.pack("!Q", ln))
            frame.extend(payload)
            with write_lock:
                conn.sendall(bytes(frame))

        while self.running:
            try:
                hdr = recv_exact(2)
                b0, b1 = hdr[0], hdr[1]
                opcode = b0 & 0x0F
                masked = (b1 & 0x80) != 0
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", recv_exact(8))[0]
                mask = recv_exact(4) if masked else None
                data = recv_exact(length) if length > 0 else b""
                if masked and mask is not None:
                    data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with write_lock:
                        conn.sendall(bytes([0x8A, len(data)]) + data)
                    continue
                if opcode != 0x1:
                    continue

                req = json.loads(data.decode("utf-8"))
                msg_id = req.get("id")
                method = req.get("method", "")
                params = req.get("params") or {}
                result = self._handle_cdp_method(tid, method, params, send_text)
                send_text({"id": msg_id, "result": result})
            except Exception:
                break

    def _handle_cdp_method(self, tid: str, method: str, params: dict, send_event):
        if method in ("Page.enable", "Runtime.enable", "DOM.enable", "Network.enable", "Log.enable", "Page.bringToFront"):
            if method == "Runtime.enable":
                send_event({
                    "method": "Runtime.consoleAPICalled",
                    "params": {
                        "type": "log",
                        "args": [{"type": "string", "value": "hello from console"}],
                        "timestamp": 1000,
                    },
                })
            if method == "Network.enable":
                send_event({
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "requestId": "net-1",
                        "type": "Document",
                        "request": {"url": "https://example.org/api", "method": "GET", "headers": {}},
                    },
                })
                send_event({
                    "method": "Network.responseReceived",
                    "params": {
                        "requestId": "net-1",
                        "type": "Document",
                        "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "application/json"}, "mimeType": "application/json"},
                    },
                })
            return {}
        if method == "Page.navigate":
            url = params.get("url", "about:blank")
            with self.lock:
                if tid in self.targets:
                    self.targets[tid]["url"] = url
            send_event({"method": "Page.loadEventFired", "params": {"timestamp": 1}})
            return {"frameId": "FRAME_1"}
        if method == "Page.reload":
            send_event({"method": "Page.loadEventFired", "params": {"timestamp": 2}})
            return {}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 1, "entries": [{"id": 10, "url": "https://example.org/prev"}, {"id": 11, "url": "https://example.org/cur"}]}
        if method == "Page.navigateToHistoryEntry":
            with self.lock:
                if tid in self.targets:
                    self.targets[tid]["url"] = "https://example.org/prev"
            send_event({"method": "Page.loadEventFired", "params": {"timestamp": 3}})
            return {}
        if method == "Page.captureScreenshot":
            png_stub = b"\x89PNG\r\n\x1a\nmockpng"
            return {"data": base64.b64encode(png_stub).decode("ascii")}
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 412, "height": 1200}}
        if method == "Page.handleJavaScriptDialog":
            self.dialog_handled.append(params)
            return {}
        if method == "Input.dispatchMouseEvent":
            self.mouse_events.append(params)
            return {}
        if method == "Input.insertText":
            self.inserted_texts.append(params.get("text", ""))
            return {}
        if method == "Input.dispatchKeyEvent":
            self.key_events.append(params)
            return {}
        if method == "Emulation.setDeviceMetricsOverride":
            self.emulated_sizes.append((params.get("width"), params.get("height")))
            return {}
        if method == "Network.getResponseBody":
            return {"body": "{\"ok\":true}", "base64Encoded": False}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if expr == "document.readyState":
                return {"result": {"type": "string", "value": "complete"}}
            if expr == "location.href":
                with self.lock:
                    u = self.targets.get(tid, {}).get("url", "about:blank")
                return {"result": {"type": "string", "value": u}}
            if "window.__agy_snapshot_refs = refs;" in expr:
                snap = 'uid=e1 RootWebArea "Initial Tab" url="https://example.org/start"\n  uid=e2 button "Sign In"\n  uid=e3 textbox "Email"'
                return {"result": {"type": "string", "value": snap}}
            if "getBoundingClientRect" in expr:
                tag = "input" if "e3" in expr else "button"
                return {
                    "result": {
                        "type": "object",
                        "value": {
                            "ok": True,
                            "tag": tag,
                            "left": 100,
                            "top": 200,
                            "width": 120,
                            "height": 40,
                            "cx": 160,
                            "cy": 220,
                        },
                    }
                }
            if "document.body ? document.body.innerText" in expr:
                return {"result": {"type": "string", "value": self.body_text}}
            if "HTMLTextAreaElement" in expr or "el.isContentEditable" in expr:
                return {"result": {"type": "boolean", "value": True}}
            if "requestAnimationFrame" in expr:
                return {"result": {"type": "boolean", "value": True}}
            if "const __fn =" in expr:
                return {"result": {"type": "object", "value": {"width": 412, "height": 915, "scale": 1}}}
            return {"result": {"type": "boolean", "value": True}}
        return {}


class TestChromeDevToolsMcp(unittest.TestCase):
    def setUp(self):
        self.cdp = MockCdpServer()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("HOME")
        self.prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["HOME"] = self.tmpdir.name
        os.environ.pop("XDG_CONFIG_HOME", None)

        cfg_dir = os.path.join(self.tmpdir.name, ".config", "google-chrome")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "DevToolsActivePort"), "w", encoding="utf-8") as fh:
            fh.write(f"{self.cdp.port}\n/devtools/browser\n")

        self.controller = mcp.BrowserController()
        self.server = mcp.McpServer(self.controller)

    def tearDown(self):
        self.controller.close()
        self.cdp.close()
        if self.prev_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.prev_home
        if self.prev_xdg is not None:
            os.environ["XDG_CONFIG_HOME"] = self.prev_xdg
        self.tmpdir.cleanup()

    def test_01_initialize_and_tools_list(self):
        init_resp = self.server.handle_message({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "chrome-devtools-mcp")

        tools_resp = self.server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools_resp["result"]["tools"]}
        expected = {
            "list_pages", "select_page", "new_page", "close_page", "navigate_page",
            "take_snapshot", "take_screenshot", "evaluate_script", "click", "click_at",
            "hover", "fill", "fill_form", "press_key", "drag", "wait_for",
            "resize_page", "handle_dialog", "list_console_messages", "get_console_message",
            "list_network_requests", "get_network_request",
        }
        self.assertEqual(names, expected)

    def test_02_list_new_select_close_pages(self):
        resp = self.server.dispatch_tool("list_pages", {})
        self.assertIn("## Pages\n1: https://example.org/start [selected]", resp["content"][0]["text"])
        self.assertEqual(resp["structuredContent"]["pages"][0]["id"], 1)
        self.assertTrue(resp["structuredContent"]["pages"][0]["selected"])

        # Complex URL with nested ?a=1&url=nested must survive encoding round-trip
        complex_url = "https://example.org/auth?a=1&url=nested_value"
        created = self.server.dispatch_tool("new_page", {"url": complex_url})
        pages = created["structuredContent"]["pages"]
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["url"], complex_url)
        self.assertTrue(pages[1]["selected"])

        selected = self.server.dispatch_tool("select_page", {"pageId": 1})
        self.assertTrue(selected["structuredContent"]["pages"][0]["selected"])
        self.assertIn("TARGET_1", self.cdp.activated_ids)

        closed = self.server.dispatch_tool("close_page", {"pageId": 2})
        self.assertEqual(len(closed["structuredContent"]["pages"]), 1)
        self.assertIn("TARGET_2", self.cdp.closed_ids)

    def test_03_navigation_snapshot_interaction_and_eval(self):
        nav = self.server.dispatch_tool("navigate_page", {"type": "url", "url": "https://example.org/login"})
        self.assertIn("https://example.org/login", nav["content"][0]["text"])

        back = self.server.dispatch_tool("navigate_page", {"type": "back"})
        self.assertIn("https://example.org/prev", back["content"][0]["text"])

        snap = self.server.dispatch_tool("take_snapshot", {})
        self.assertIn('uid=e2 button "Sign In"', snap["content"][0]["text"])

        click_res = self.server.dispatch_tool("click", {"uid": "e2", "includeSnapshot": True})
        self.assertIn("Successfully clicked element e2", click_res["content"][0]["text"])
        self.assertIn("uid=e2", click_res["content"][0]["text"])
        self.assertTrue(any(ev["type"] == "mousePressed" and ev["x"] == 160 for ev in self.cdp.mouse_events))

        self.server.dispatch_tool("fill", {"uid": "e3", "value": "user@example.com"})
        self.assertEqual(self.cdp.inserted_texts[-1], "user@example.com")

        self.server.dispatch_tool("press_key", {"key": "Control+Enter"})
        self.assertTrue(any(ev["key"] == "Enter" and ev["modifiers"] == 2 for ev in self.cdp.key_events))

        eval_res = self.server.dispatch_tool("evaluate_script", {"function": "() => ({width: 412})"})
        eval_text = eval_res["content"][0]["text"]
        match = re.search(r"```json\n(.*?)\n```", eval_text, re.DOTALL)
        self.assertIsNotNone(match, f"Missing ```json fence in evaluate_script output: {eval_text}")
        parsed = json.loads(match.group(1))
        self.assertEqual(parsed["width"], 412)

    def test_04_screenshot_console_network_dialog_and_wait(self):
        shot = self.server.dispatch_tool("take_screenshot", {"format": "png", "fullPage": True})
        self.assertEqual(shot["content"][1]["type"], "image")
        self.assertEqual(shot["content"][1]["mimeType"], "image/png")
        decoded = base64.b64decode(shot["content"][1]["data"])
        self.assertTrue(decoded.startswith(b"\x89PNG"))

        shot_file = os.path.join(self.tmpdir.name, "sub", "shot.png")
        self.server.dispatch_tool("take_screenshot", {"filePath": shot_file})
        self.assertTrue(os.path.isfile(shot_file))

        # Trigger event delivery by running wait_for
        wait_res = self.server.dispatch_tool("wait_for", {"text": ["Agent Browser"], "timeout": 1000})
        self.assertIn("Agent Browser", wait_res["content"][0]["text"])

        console_list = self.server.dispatch_tool("list_console_messages", {})
        self.assertIn("hello from console", console_list["content"][0]["text"])
        console_detail = self.server.dispatch_tool("get_console_message", {"msgid": 1})
        self.assertIn("hello from console", console_detail["content"][0]["text"])

        net_list = self.server.dispatch_tool("list_network_requests", {})
        self.assertIn("https://example.org/api [200]", net_list["content"][0]["text"])
        net_detail = self.server.dispatch_tool("get_network_request", {"reqid": 1})
        self.assertIn('\\"ok\\":true', net_detail["content"][0]["text"])

        self.server.dispatch_tool("resize_page", {"width": 390, "height": 844})
        self.assertEqual(self.cdp.emulated_sizes[-1], (390, 844))

        self.server.dispatch_tool("handle_dialog", {"action": "accept", "promptText": "yes"})
        self.assertEqual(self.cdp.dialog_handled[-1], {"accept": True, "promptText": "yes"})

    def test_05_stdio_subprocess_roundtrip(self):
        requests = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_pages", "arguments": {}}}),
        ]
        proc = subprocess.run(
            [sys.executable, SHIM_PATH, "--no-usage-statistics", "--no-performance-crux", "--autoConnect"],
            input="\n".join(requests) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
            env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out_lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
        self.assertEqual(len(out_lines), 2)
        self.assertEqual(out_lines[0]["id"], 1)
        self.assertEqual(out_lines[1]["id"], 2)
        self.assertIn("https://example.org/start", out_lines[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
