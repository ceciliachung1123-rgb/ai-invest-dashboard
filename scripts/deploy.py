#!/usr/bin/env python3
"""
AI 投资看板 - 部署脚本（2026-10-23 验证版）

用法：
  python3 deploy.py morning 2026-10-24
  python3 deploy.py evening 2026-10-24
  python3 deploy.py weekly  2026-10-25

关键特性（全部经过线上实测）：
1. 从 GitHub 远端拉基线（sandbox 本地是空壳，直接读会覆盖历史）
2. 按 id 去重/替换，追加新报告
3. slim 自动收紧：保证 v3.json ≤ 85000 bytes（CDN >100KB 会截断）
4. compact 序列化（indent=2 会膨胀 25%）
5. 只 update 已有文件，PUT 带 sha（create 新文件名会触发 Page build failed）
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path("/workspace/ai-investment-dashboard")
os.chdir(BASE)

API = "https://api.github.com/repos/ceciliachung1123-rgb/ai-invest-dashboard"
HDRS = {
    "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
    "Accept": "application/vnd.github+json",
}
SIZE_LIMIT = 85000  # CDN 截断阈值 100KB，留 15% 安全余量
DEPLOY_FILES = ["dashboard-data-v3.json", "data/dashboard-data.json"]


def pull_remote(rel):
    """从 GitHub 拉远端文件内容（返回 bytes），不存在则返回 None"""
    req = urllib.request.Request(f"{API}/contents/{rel}", headers=HDRS)
    try:
        with urllib.request.urlopen(req) as resp:
            meta = json.loads(resp.read())
            return base64.b64decode(meta["content"]), meta["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def write_compact(obj, path):
    """compact 序列化：indent=2 会膨胀 25%，必须用 separators"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return p.stat().st_size


def slim(dd, m_lim, e_lim, w_lim):
    """保留最近 N 条（d 已按 date 升序，[-N:] = 最近 N 条）"""
    out = dict(dd)
    out["morning"] = dd.get("morning", [])[-m_lim:]
    out["evening"] = dd.get("evening", [])[-e_lim:]
    out["weekly"] = dd.get("weekly", [])[-w_lim:]
    return out


def main():
    rtype = sys.argv[1] if len(sys.argv) > 1 else "morning"
    date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")

    print(f"=== 部署 {rtype} {date} ===")

    # 1) 拉远端基线
    remote_bytes = {}
    shas = {}
    for rel in DEPLOY_FILES:
        content, sha = pull_remote(rel)
        if content is None:
            print(f"WARN: {rel} 远端不存在，将用空对象初始化")
            remote_bytes[rel] = b'{"morning":[],"evening":[],"weekly":[]}'
        else:
            remote_bytes[rel] = content
            shas[rel] = sha
            print(f"pulled {rel}: {len(content)} bytes sha={sha[:8]}")
        local = BASE / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(remote_bytes[rel])

    # 2) 合并新报告
    src_file = BASE / "data" / f"{rtype}-{date}.json"
    if not src_file.exists():
        print(f"ERROR: 找不到 {src_file}")
        return 1
    new = json.load(open(src_file, encoding="utf-8"))
    key = {"morning": "morning", "evening": "evening", "weekly": "weekly"}[rtype]
    new_item = new[key][0]

    d = json.loads(remote_bytes["dashboard-data-v3.json"].decode("utf-8"))
    d.setdefault("morning", [])
    d.setdefault("evening", [])
    d.setdefault("weekly", [])

    existing = {x.get("id") for x in d[key]}
    if new_item["id"] in existing:
        for i, x in enumerate(d[key]):
            if x.get("id") == new_item["id"]:
                d[key][i] = new_item
                print(f"updated {new_item['id']}")
                break
    else:
        d[key].append(new_item)
        print(f"added {new_item['id']} (total {len(d[key])})")

    for k in ("morning", "evening", "weekly"):
        d[k].sort(key=lambda x: x.get("date", ""))
    d["updated_at"] = datetime.now().isoformat(timespec="seconds")
    d.pop("_force_deploy", None)
    d.pop("_deploy_marker", None)

    # 3) slim 自动收紧到 ≤ SIZE_LIMIT
    chosen = None
    for m_lim, e_lim, w_lim in [(6, 6, 3), (5, 5, 2), (4, 4, 2), (3, 3, 2), (2, 2, 2)]:
        cand = slim(d, m_lim, e_lim, w_lim)
        size = write_compact(cand, "dashboard-data-v3.json")
        print(f"[slim] {m_lim}+{e_lim}+{w_lim} -> {size} bytes")
        if size <= SIZE_LIMIT:
            d = cand
            chosen = (m_lim, e_lim, w_lim)
            break
    else:
        d = cand
        chosen = (2, 2, 2)
        print(f"[slim] WARN 最小组合仍 {size} > {SIZE_LIMIT}，强制使用")

    write_compact(d, "data/dashboard-data.json")
    final_size = write_compact(d, "dashboard-data-v3.json")
    print(f"[slim] final: {chosen[0]}+{chosen[1]}+{chosen[2]} = {final_size} bytes")
    print(f"[counts] m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])}")
    if final_size > SIZE_LIMIT:
        print(f"[ERROR] 最终体积 {final_size} > {SIZE_LIMIT}，CDN 可能截断！")

    # 4) PUT 上传（只 update 已有文件，带 sha）
    for rel in DEPLOY_FILES:
        api_url = f"{API}/contents/{rel}"
        req = urllib.request.Request(api_url, headers=HDRS)
        sha = None
        try:
            with urllib.request.urlopen(req) as resp:
                sha = json.loads(resp.read())["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        if sha is None:
            print(f"SKIP {rel}: 远端不存在（禁止 create 新文件）")
            continue
        local = BASE / rel
        body = {
            "message": f"feat: add {rtype} {date} (m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])})",
            "content": base64.b64encode(local.read_bytes()).decode(),
            "sha": sha,
        }
        put_req = urllib.request.Request(
            api_url,
            data=json.dumps(body).encode("utf-8"),
            headers={**HDRS, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(put_req) as resp:
            r = json.loads(resp.read())
            print(f"PUT {rel}: {r['commit']['sha'][:10]} ({r['content']['size']} bytes)")

    print("=== 完成 ===")
    print("验证：curl -sI https://ceciliachung1123-rgb.github.io/ai-invest-dashboard/dashboard-data-v3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
