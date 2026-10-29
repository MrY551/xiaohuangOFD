# -*- coding: utf-8 -*-
"""网页端截图（真实浏览器，带选中态）。

    python tools/web_shot.py --port 8760 --out out/shot_web.png

需要网页端已在运行：
    python web/server.py --open out/sample.ofd
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from cdp_web_test import CDP  # 复用最小 WebSocket + CDP 客户端


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--cdp-port", type=int, default=9334)
    ap.add_argument("--browser",
                    default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "shot_web.png"))
    ap.add_argument("--page", type=int, default=0)
    args = ap.parse_args(argv)

    url = "http://127.0.0.1:%d/" % args.port
    try:
        urllib.request.urlopen(url, timeout=5)
    except Exception as exc:
        print("网页端没起来：%s\n先运行: python web/server.py --open out/sample.ofd" % exc)
        return 2

    profile = os.path.join(ROOT, "out", ".cdp-profile-shot")
    proc = subprocess.Popen([
        args.browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--window-size=1440,940",
        "--remote-debugging-port=%d" % args.cdp_port,
        "--user-data-dir=" + os.path.abspath(profile), url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        proc.kill()
        print("拿不到 CDP 目标")
        return 1

    cdp = CDP(target["webSocketDebuggerUrl"])
    try:
        cdp.call("Runtime.enable")
        for _ in range(80):
            try:
                if cdp.eval("window.__OFD__ && window.__OFD__.ready()"):
                    break
            except Exception:
                pass
            time.sleep(0.2)
        # 切页 → 选中标题 → 等重绘
        cdp.eval("""(async () => {
            const o = window.__OFD__;
            const idx = %d;
            if (idx > 0) {
                document.querySelectorAll('.thumb')[idx].click();
                await new Promise(r => setTimeout(r, 700));
            }
            const obj = o.state.page.objects.find(x => x.type === '文本');
            if (obj) o.selectById(obj.id);
            await new Promise(r => setTimeout(r, 200));
            return true;
        })()""" % args.page)
        time.sleep(0.4)
        data = cdp.call("Page.captureScreenshot", {"format": "png"})
        raw = base64.b64decode(data["data"])
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "wb") as fh:
            fh.write(raw)
        print("写出", args.out, len(raw), "字节")
    finally:
        cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 清掉临时 profile
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
