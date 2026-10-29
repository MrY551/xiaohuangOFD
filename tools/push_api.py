# -*- coding: utf-8 -*-
"""把当前仓库推送到 GitHub —— 走 REST API，绕开 git 传输层。

为什么不用 git push：
  这台机器 github.com:443 不通（Windows 与 WSL 都被 reset），
  可用的 ghfast.top / ghproxy.net 只做只读加速，不支持 push。
  但 api.github.com 是通的，所以改用 Git Data API：
    blob -> tree -> commit -> ref

用法：
    python tools/push_api.py --repo MrY551/xiaohuangOFD --branch main
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED = os.path.join(os.path.expanduser("~"), ".git-credentials")


def token() -> str:
    for line in open(CRED, encoding="utf-8"):
        line = line.strip()
        if "@github.com" in line and line.startswith("https://"):
            head, _, _ = line.rpartition("@")
            user_pass = head[len("https://"):]
            if ":" in user_pass:
                return user_pass.split(":", 1)[1]
    raise SystemExit("没在 %s 里找到 github.com 的凭据" % CRED)


TOK = token()


def api(path: str, method: str = "GET", body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "token " + TOK,
            "Accept": "application/vnd.github+json",
            "User-Agent": "xiaohuang-ofd-push",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"message": raw[:300]}


def tracked_files() -> list:
    """用 git ls-files 取已跟踪列表，排除规则与 .gitignore 一致。"""
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"],
                         cwd=ROOT, capture_output=True, encoding="utf-8").stdout
    return [p for p in out.splitlines() if p.strip()]


TEXT_EXT = (".py", ".md", ".js", ".mjs", ".css", ".html", ".sh", ".toml",
            ".yml", ".yaml", ".xsd", ".txt", ".gitignore", ".gitattributes")


def file_bytes(rel: str) -> bytes:
    """读文件字节。文本归一化成 LF —— 必须与 local_blob_sha 用同一套规则，
    否则本地算出的 sha 与远程存的 blob 永远对不上，增量推送就永远全量。"""
    data = open(os.path.join(ROOT, rel), "rb").read()
    base = os.path.basename(rel)
    if rel.endswith(TEXT_EXT) or base in (".gitignore", ".gitattributes"):
        data = data.replace(b"\r\n", b"\n")
    return data


def make_blob(rel: str):
    full = os.path.join(ROOT, rel)
    data = file_bytes(rel)
    payload = {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    }
    code, res = api("/repos/%s/git/blobs" % REPO, "POST", payload)
    if code not in (200, 201):
        raise RuntimeError("blob 失败 %s: %s %s" % (rel, code, res.get("message")))
    return rel, res["sha"]


def main(argv=None):
    global REPO
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message-file", default="",
                    help="不填则取本地最新 commit 的消息")
    ap.add_argument("--reinit", action="store_true",
                    help="重建为单提交历史（parents 为空，force 更新 ref）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--full", action="store_true", help="不做增量，全部重传")
    ap.add_argument("--force-commit", action="store_true", help="无变化也创建提交")
    args = ap.parse_args(argv)
    REPO = args.repo

    if args.message_file:
        msg_path = os.path.join(ROOT, args.message_file)
        message = open(msg_path, encoding="utf-8").read()
    else:
        message = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=ROOT,
                                 capture_output=True, encoding="utf-8").stdout.strip()

    files = tracked_files()
    print("待推送文件: %d 个" % len(files))

    # 空仓库不能建 blob（API 返回 409），先用 Contents API 放一个文件立初始提交
    parent = None
    code, ref0 = api("/repos/%s/git/ref/heads/%s" % (REPO, args.branch))
    if code != 200:
        seed = "README.md"
        seed_full = os.path.join(ROOT, seed)
        content = open(seed_full, "rb").read() if os.path.exists(seed_full) else b"# xiaohuangOFD\n"
        code, res = api("/repos/%s/contents/%s" % (REPO, seed), "PUT", {
            "message": "初始化仓库",
            "content": base64.b64encode(content).decode("ascii"),
        })
        if code not in (200, 201):
            raise SystemExit("初始提交失败: %s %s" % (code, res.get("message")))
        parent = res["commit"]["sha"]
        print("初始提交:", parent[:12])
    else:
        parent = ref0["object"]["sha"]
        print("已有分支，parent:", parent[:12])

    # 1) 先读远程 tree，内容没变的文件复用远程 sha（增量推送）
    remote_blobs = {}
    if not args.full:
        code, tr = api("/repos/%s/git/trees/%s?recursive=1" % (REPO, args.branch))
        if code == 200:
            remote_blobs = {n["path"]: n["sha"] for n in tr.get("tree", [])
                            if n.get("type") == "blob"}
            print("远程已有 %d 个 blob，做增量比对" % len(remote_blobs))

    def local_blob_sha(path):
        import hashlib
        data = file_bytes(path)
        h = hashlib.sha1(b"blob %d\0" % len(data))
        h.update(data)
        return h.hexdigest()

    entries, todo = [], []
    for f in files:
        rel = f.replace(os.sep, "/")
        local = local_blob_sha(os.path.join(ROOT, f))
        if remote_blobs.get(rel) == local:
            entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": local})
        else:
            todo.append((rel, f, local))
    print("未变 %d 个，需上传 %d 个" % (len(entries), len(todo)))

    # 没有任何变化就不要产生空提交（除非显式 --force-commit）
    if not todo and not args.reinit and not args.force_commit:
        remote_tree = api("/repos/%s/git/trees/%s?recursive=1" % (REPO, args.branch))[1]
        same = sorted(n["path"] for n in remote_tree.get("tree", [])
                      if n.get("type") == "blob")
        mine = sorted(e["path"] for e in entries)
        if same == mine:
            print("内容无变化，跳过提交（远程已是这份内容）")
            return 0

    # 2) 只对变化的文件建 blob；本地算出的 sha 与远程一致，不必再取回
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(make_blob, f): (rel, local) for rel, f, local in todo}
        done = 0
        for fu in futs:
            rel, _ = futs[fu]
            _r, sha = fu.result()
            entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
            done += 1
            print("  blob %d/%d  %s" % (done, len(todo), rel))
    entries.sort(key=lambda e: e["path"])

    # 2) 建 tree
    code, tree = api("/repos/%s/git/trees" % REPO, "POST", {"tree": entries})
    if code not in (200, 201):
        raise SystemExit("tree 失败: %s %s" % (code, tree.get("message")))
    print("tree:", tree["sha"][:12])

    # 3) 建 commit
    parents = [] if args.reinit else ([parent] if parent else [])
    code, cm = api("/repos/%s/git/commits" % REPO, "POST",
                   {"message": message, "tree": tree["sha"], "parents": parents})
    if code not in (200, 201):
        raise SystemExit("commit 失败: %s %s" % (code, cm.get("message")))
    print("commit:", cm["sha"][:12])

    # 4) 更新或创建 ref
    code, ref = api("/repos/%s/git/ref/heads/%s" % (REPO, args.branch))
    if code == 200:
        code, res = api("/repos/%s/git/refs/heads/%s" % (REPO, args.branch),
                        "PATCH", {"sha": cm["sha"], "force": bool(args.reinit)})
    else:
        code, res = api("/repos/%s/git/refs" % REPO, "POST",
                        {"ref": "refs/heads/" + args.branch, "sha": cm["sha"]})
    if code not in (200, 201):
        raise SystemExit("ref 失败: %s %s" % (code, res.get("message")))
    print("ref:", res.get("ref"), "->", res.get("object", {}).get("sha", "")[:12])

    # 5) 复核
    code, info = api("/repos/%s" % REPO)
    print("仓库 size:", info.get("size"), "KB  pushed_at:", info.get("pushed_at"))
    code, br = api("/repos/%s/branches/%s" % (REPO, args.branch))
    if code == 200:
        print("分支 %s 指向: %s" % (args.branch, br["commit"]["sha"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
