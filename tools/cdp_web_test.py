# -*- coding: utf-8 -*-
"""用真实浏览器（Edge/Chrome headless + CDP）验证网页端交互。

    python tools/cdp_web_test.py --port 8760 --browser "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"

做的是真事：起 CDP、打开页面、执行 JS 点选/拖动/改文字，读回状态。
不依赖 selenium / playwright，只用标准库 + 最小 WebSocket 客户端。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print("  ok   %s" % name)
        except Exception as exc:
            FAIL.append((name, exc))
            print("  FAIL %s: %s" % (name, exc))
        return fn
    return deco


# ------------------------------------------------------------------ 最小 WS

class WS(object):
    def __init__(self, url: str, timeout: float = 20.0):
        assert url.startswith("ws://"), url
        rest = url[len("ws://"):]
        host_port, path = rest.split("/", 1)
        path = "/" + path
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手被拒: %r" % resp[:120])

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("连接关闭")
            buf += chunk
        return buf

    def recv(self) -> str:
        while True:
            h = self._read_exact(2)
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            n = h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:      # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x0):
                return payload.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class CDP(object):
    def __init__(self, ws_url: str):
        self.ws = WS(ws_url)
        self._id = 0

    def call(self, method: str, params=None, timeout: float = 30.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result")
        raise RuntimeError("%s 超时" % method)

    def eval(self, expr: str, await_promise: bool = True):
        res = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        })
        if res.get("exceptionDetails"):
            raise RuntimeError("JS 异常: %s" % json.dumps(
                res["exceptionDetails"], ensure_ascii=False)[:400])
        return res.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ------------------------------------------------------------------ 测试

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8760, help="网页端服务端口")
    ap.add_argument("--cdp-port", type=int, default=9333)
    ap.add_argument("--browser", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    ap.add_argument("--shot", default="out/web_interact.png")
    args = ap.parse_args(argv)

    url = "http://127.0.0.1:%d/" % args.port
    try:
        urllib.request.urlopen(url, timeout=5)
    except Exception as exc:
        print("网页端没起来（%s）：先运行 python web/server.py --open out/sample.ofd" % exc)
        return 2

    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out",
                           "cdp-profile")
    proc = subprocess.Popen([
        args.browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--window-size=1440,920",
        "--remote-debugging-port=%d" % args.cdp_port,
        "--user-data-dir=" + os.path.abspath(profile),
        url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cdp = None
    target = None
    for _ in range(120):
        time.sleep(0.25)
        try:
            data = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % args.cdp_port, timeout=3).read())
            pages = [t for t in data if t.get("type") == "page"
                     and t.get("url", "").startswith("http")]
            if pages:
                target = pages[0]
                break
        except Exception:
            continue
    if target is None:
        print("拿不到 CDP 目标")
        proc.kill()
        return 1

    try:
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        # 等页面初始化完成
        for _ in range(80):
            try:
                if cdp.eval("window.__OFD__ && window.__OFD__.ready()"):
                    break
            except Exception:
                pass
            time.sleep(0.2)

        state = {}

        @check("页面初始化：画布与场景就绪")
        def _():
            info = cdp.eval("""(() => {
                const o = window.__OFD__;
                return {
                    ready: o.ready(),
                    ops: o.state.page.ops.length,
                    objects: o.state.page.objects.length,
                    size: o.state.page.size,
                    canvas: [document.getElementById('cv').width,
                             document.getElementById('cv').height],
                    thumbs: document.querySelectorAll('.thumb').length,
                    emptystateHidden: document.getElementById('emptystate').hidden,
                };
            })()""")
            if not info["ready"] or info["ops"] < 5:
                raise AssertionError(info)
            if info["thumbs"] != 2:
                raise AssertionError("缩略图数量 %r" % info["thumbs"])
            if not info["emptystateHidden"]:
                raise AssertionError("空态没隐藏")
            if info["canvas"][0] < 300:
                raise AssertionError(info["canvas"])
            state["info"] = info

        @check("命中测试：点在标题上选中文本对象")
        def _():
            r = cdp.eval("""(() => {
                const o = window.__OFD__;
                const hit = o.hit(40, 30);      // 标题文字附近
                const miss = o.hit(5, 290);     // 页脚空白
                return {hit: hit && {id: hit.id, type: hit.type, text: hit.text},
                        miss: miss && miss.id};
            })()""")
            if not r["hit"]:
                raise AssertionError("标题上没命中任何对象")
            if r["hit"]["type"] != "文本":
                raise AssertionError(r["hit"])
            if r["miss"]:
                raise AssertionError("空白处不该命中 %r" % r["miss"])
            state["obj"] = r["hit"]

        @check("真实鼠标点击 → 选中 + 属性面板更新")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const cv = document.getElementById('cv');
                const rect = cv.getBoundingClientRect();
                const Z = 96 / 25.4 * o.state.zoom;
                // 把页面坐标 (40, 30) 换算成屏幕坐标
                const x = rect.left + 40 * Z;
                const y = rect.top + 30 * Z;
                const opts = {bubbles: true, cancelable: true, clientX: x, clientY: y,
                              pointerId: 1, pointerType: 'mouse', button: 0, buttons: 1};
                cv.dispatchEvent(new PointerEvent('pointerdown', opts));
                window.dispatchEvent(new PointerEvent('pointerup', opts));
                await new Promise(r => setTimeout(r, 60));
                return {
                    sel: o.state.selection.map(s => s.id),
                    props: document.getElementById('propsBody').innerText.slice(0, 80),
                };
            })()""")
            if not r["sel"]:
                raise AssertionError("点击后没有选中: %r" % r)
            if "类型" not in r["props"]:
                raise AssertionError("属性面板没更新: %r" % r["props"])
            state["sel"] = r["sel"]

        @check("拖动对象 → 服务端落位并回读坐标")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const obj = o.state.selection[0];
                const before = obj.corners[0][0];
                const cv = document.getElementById('cv');
                const rect = cv.getBoundingClientRect();
                const Z = 96 / 25.4 * o.state.zoom;
                // 从对象中心按下：文本对象的外接矩形左上角可能落在字形之外，
                // 从角上按下会命中不到（命中测试用的是字形/内容框）
                const cx = (obj.corners[0][0] + obj.corners[2][0]) / 2;
                const cy = (obj.corners[0][1] + obj.corners[2][1]) / 2;
                const sx = rect.left + cx * Z;
                const sy = rect.top + cy * Z;
                const down = {bubbles: true, clientX: sx, clientY: sy, pointerId: 2,
                              pointerType: 'mouse', button: 0, buttons: 1};
                cv.dispatchEvent(new PointerEvent('pointerdown', down));
                const move = Object.assign({}, down, {clientX: sx + 12 * Z, clientY: sy});
                window.dispatchEvent(new PointerEvent('pointermove', move));
                await new Promise(r => setTimeout(r, 40));
                window.dispatchEvent(new PointerEvent('pointerup', move));
                // 服务端编辑 + 重新载入是异步的，冷启动时可能超过 600ms：
                // 轮询等它落位，别用固定 sleep 判失败
                let after = null;
                for (let i = 0; i < 60; i++) {
                  await new Promise(r => setTimeout(r, 100));
                  const cur = o.state.page.objects.find(x => x.id === obj.id);
                  if (cur && Math.abs(cur.corners[0][0] - before) > 0.5) { after = cur; break; }
                }
                return {before, after: after ? after.corners[0][0] : null,
                        objects: o.state.page.objects.length};
            })()""")
            if r["after"] is None:
                raise AssertionError("拖动后对象丢了")
            if abs((r["after"] - r["before"]) - 12) > 0.6:
                raise AssertionError("位移不对 before=%s after=%s" % (r["before"], r["after"]))

        @check("双击文本打开编辑框")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const cv = document.getElementById('cv');
                const rect = cv.getBoundingClientRect();
                const Z = 96 / 25.4 * o.state.zoom;
                const obj = o.state.page.objects.find(x => x.type === '文本');
                const sx = rect.left + (obj.corners[0][0] + 2) * Z;
                const sy = rect.top + (obj.corners[0][1] + 3) * Z;
                cv.dispatchEvent(new MouseEvent('dblclick', {bubbles: true,
                    clientX: sx, clientY: sy, button: 0}));
                await new Promise(r => setTimeout(r, 120));
                const dlg = document.getElementById('dlgText');
                const val = document.getElementById('textInput').value;
                return {open: !dlg.hidden, value: val};
            })()""")
            if not r["open"]:
                raise AssertionError("编辑框没弹出来")
            if not r["value"]:
                raise AssertionError("编辑框里没有原文字")
            cdp.eval("document.getElementById('dlgText').hidden = true; true")

        @check("从界面改文字并提交")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const obj = o.state.page.objects.find(x => x.type === '文本');
                o.selectById(obj.id);
                document.getElementById('textInput').value = '浏览器里改的标题';
                document.getElementById('textSize').value = '11';
                document.getElementById('textColor').value = '#C81E1E';
                const dlg = document.getElementById('dlgText');
                dlg.hidden = false;
                document.getElementById('textOk').click();
                await new Promise(r => setTimeout(r, 900));
                const after = o.state.page.objects.find(x => x.id === obj.id);
                return {text: after && after.text, size: after && after.size,
                        color: after && after.color, hidden: dlg.hidden};
            })()""")
            if r["text"] != "浏览器里改的标题":
                raise AssertionError(r)
            if abs((r["size"] or 0) - 11) > 0.05:
                raise AssertionError("字号 %r" % r["size"])
            if not r["hidden"]:
                raise AssertionError("提交后弹窗没关")

        @check("插入文字按钮 → 点击页面 → 新对象出现")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const n0 = o.state.page.objects.length;
                document.getElementById('btnText').click();
                const cv = document.getElementById('cv');
                const rect = cv.getBoundingClientRect();
                const Z = 96 / 25.4 * o.state.zoom;
                const opts = {bubbles: true, clientX: rect.left + 60 * Z,
                              clientY: rect.top + 240 * Z, pointerId: 3,
                              pointerType: 'mouse', button: 0, buttons: 1};
                cv.dispatchEvent(new PointerEvent('pointerdown', opts));
                await new Promise(r => setTimeout(r, 900));
                return {n0, n1: o.state.page.objects.length,
                        mode: o.state.mode};
            })()""")
            if r["n1"] != r["n0"] + 1:
                raise AssertionError(r)

        @check("撤销按钮回到插入前")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                const n0 = o.state.page.objects.length;
                document.getElementById('btnUndo').click();
                await new Promise(r => setTimeout(r, 800));
                return {n0, n1: o.state.page.objects.length,
                        canRedo: o.state.doc.can_redo};
            })()""")
            if r["n1"] != r["n0"] - 1:
                raise AssertionError(r)
            if not r["canRedo"]:
                raise AssertionError("重做按钮应可用")

        @check("切到第 2 页渲染表格页")
        def _():
            r = cdp.eval("""(async () => {
                const o = window.__OFD__;
                document.querySelectorAll('.thumb')[1].click();
                await new Promise(res => setTimeout(res, 900));
                return {index: o.state.pageIndex,
                        objects: o.state.page.objects.length,
                        hasPath: o.state.page.ops.some(x => x.t === 'path'),
                        activeThumb: document.querySelector('.thumb.active span').innerText};
            })()""")
            if r["index"] != 1 or not r["hasPath"]:
                raise AssertionError(r)
            if "第 2 页" not in r["activeThumb"]:
                raise AssertionError(r["activeThumb"])

        @check("画布截图")
        def _():
            data = cdp.call("Page.captureScreenshot", {"format": "png"})
            raw = base64.b64decode(data["data"])
            if len(raw) < 8000:
                raise AssertionError("截图太小 %d" % len(raw))
            path = os.path.abspath(args.shot)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(raw)
            print("     截图:", path)

    finally:
        if cdp:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        for name, exc in FAIL:
            print("  -", name, ":", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
