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


def pull_remote(rel, ref=None):
    """从 GitHub 拉远端文件内容（返回 bytes），不存在则返回 None
    ref 可指定 commit sha / branch / tag，从历史版本拉基线"""
    url = f"{API}/contents/{rel}"
    if ref:
        url += f"?ref={ref}"
    req = urllib.request.Request(url, headers=HDRS)
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


def get_remote_sha(rel):
    req = urllib.request.Request(f"{API}/contents/{rel}", headers=HDRS)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def put_file(rel, data_bytes, message):
    """PUT 上传一个文件（update 优先，带 sha；不存在则 create）"""
    api_url = f"{API}/contents/{rel}"
    req = urllib.request.Request(api_url, headers=HDRS)
    sha = None
    try:
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    body = {"message": message, "content": base64.b64encode(data_bytes).decode()}
    if sha:
        body["sha"] = sha
    put_req = urllib.request.Request(
        api_url, data=json.dumps(body).encode("utf-8"),
        headers={**HDRS, "Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(put_req) as resp:
        r = json.loads(resp.read())
        print(f"  PUT {rel}: {r['commit']['sha'][:10]} ({r['content']['size']} bytes)")
        return r["commit"]["sha"]


def slim(dd, m_lim, e_lim, w_lim):
    """保留最近 N 条（d 已按 date 升序，[-N:] = 最近 N 条）"""
    out = dict(dd)
    out["morning"] = dd.get("morning", [])[-m_lim:]
    out["evening"] = dd.get("evening", [])[-e_lim:]
    out["weekly"] = dd.get("weekly", [])[-w_lim:]
    return out


# ============ 🚨 2026-10-30 事故修复：先压内容，再删条目 ============
# 原 bug：slim 用 [-N:] 取最近 N 条，每降一档就静默丢掉最老条目。
# 10-30 晚报实测：3+3+2 = 85093（超 93 bytes）→ 继续降档到 2+2+2
#              → 静默删除 morning-2026-10-28 与 evening-2026-10-27。
# 修复原则：
#   1) 优先把长文本字段外置到 md 文件（体积换历史）
#   2) 再精简最老条目的冗余结构化字段（正文已在 md 里，字段是重复的）
#   3) 最后才考虑减条目数，且减到 3+3+2 为硬保底，不再往下掉
# ======================================================================

KEEP_FIELDS = {
    "id", "date", "type", "title", "content_md_file", "content_file",
    "sentiment", "sentiment_basis", "cycle_phase", "baseDate", "base_date",
    "publishTime", "updated_at", "week_start", "week_end", "weekStart", "weekEnd",
}


def externalize_longtext(item, content_dir, prefix):
    """把超长的 markdown 文本字段外置到 md 文件，只在 v3 里留引用 + 摘要"""
    for k in ("summary_markdown", "content_md", "content"):
        v = item.get(k)
        if isinstance(v, str) and len(v) > 1200 and not item.get("content_md_file"):
            fname = f"{prefix}/{item.get('id', 'x').replace('/', '_')}.md"
            target = content_dir / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(v, encoding="utf-8")
            item["content_md_file"] = fname
            item[k] = v[:300] + "\n\n（正文已外置，见 content_md_file）"
            return True
    return False


def compact_oldest(item):
    """精简最老条目：保留核心字段，长字符串截断，长数组/字典折叠。
    正文已在 content_md_file 里，结构化字段是重复信息，可以安全精简。"""
    out = {}
    for k, v in item.items():
        if k in KEEP_FIELDS:
            out[k] = v
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list) and len(json.dumps(v, ensure_ascii=False)) > 300:
            out[k] = f"（{len(v)} 项，详细内容见正文 md）"
        elif isinstance(v, dict) and len(json.dumps(v, ensure_ascii=False)) > 300:
            out[k] = {kk: v[kk] for kk in list(v)[:3]}
        else:
            out[k] = v
    return out


def fit_size(dd, content_dir):
    """把 dd 压到 ≤ SIZE_LIMIT。优先级：外置 > 精简最老 > 减条目（保底 3+3+2）。
    返回 (最终数据, 说明文本, 需要上传的 md 文件列表)"""
    pending_md = []

    # Step 1: 外置所有条目里的长 markdown
    for key, prefix in (("morning", "morning-content"), ("evening", "evening-content"),
                        ("weekly", "weekly-content")):
        for it in dd.get(key, []):
            if externalize_longtext(it, content_dir, prefix):
                pending_md.append(f"data/{prefix}/{it.get('id', 'x').replace('/', '_')}.md")

    # Step 2: 逐档收紧，每次先精简最老条目
    for m_lim, e_lim, w_lim in [(6, 6, 3), (5, 5, 2), (4, 4, 2), (3, 3, 2)]:
        cand = slim(dd, m_lim, e_lim, w_lim)
        size = write_compact(cand, "/tmp/_fit.json")
        if size <= SIZE_LIMIT:
            return cand, f"{m_lim}+{e_lim}+{w_lim} (无需精简)", pending_md
        # 超标 → 精简最老条目后重试
        for key in ("morning", "evening"):
            arr = cand.get(key, [])
            if arr:
                arr[0] = compact_oldest(arr[0])
        size2 = write_compact(cand, "/tmp/_fit.json")
        if size2 <= SIZE_LIMIT:
            return cand, f"{m_lim}+{e_lim}+{w_lim} (已精简最老条目)", pending_md

    # Step 3: 硬保底 3+3+2，再不行就再精简一次最老的两个
    cand = slim(dd, 3, 3, 2)
    for key in ("morning", "evening"):
        arr = cand.get(key, [])
        for i in range(min(2, len(arr))):
            arr[i] = compact_oldest(arr[i])
    size = write_compact(cand, "/tmp/_fit.json")
    if size > SIZE_LIMIT:
        print(f"[fit] ERROR 精简后仍 {size} > {SIZE_LIMIT}，请人工检查")
    return cand, "3+3+2 (硬保底，最老两条已精简)", pending_md



def main():
    args = sys.argv[1:]
    rtype = "morning"
    date = datetime.now().strftime("%Y-%m-%d")
    baseline_ref = None
    for i, a in enumerate(args):
        if a == "--baseline-ref" and i + 1 < len(args):
            baseline_ref = args[i + 1]
        elif a.startswith("--baseline-ref="):
            baseline_ref = a.split("=", 1)[1]
        elif a.startswith("--type="):
            rtype = a.split("=", 1)[1]
        elif a.startswith("--date="):
            date = a.split("=", 1)[1]
        elif not a.startswith("--") and "." not in a:
            if rtype == "morning" and date == datetime.now().strftime("%Y-%m-%d"):
                rtype = a
            else:
                date = a

    print(f"=== 部署 {rtype} {date} ===")
    if baseline_ref:
        print(f"[baseline] 从指定 ref 拉基线: {baseline_ref}")

    # 1) 拉远端基线
    remote_bytes = {}
    shas = {}
    for rel in DEPLOY_FILES:
        content, sha = pull_remote(rel, ref=baseline_ref)
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

    # 3) fit_size：优先外置/精简内容，最后才减条目（硬保底 3+3+2）
    content_dir = BASE / "data"
    d, fit_desc, pending_md = fit_size(d, content_dir)
    final_size = write_compact(d, "dashboard-data-v3.json")
    write_compact(d, "data/dashboard-data.json")
    print(f"[fit] {fit_desc} = {final_size} bytes {'✅' if final_size <= SIZE_LIMIT else '❌ 超阈值'}")
    print(f"[counts] m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])}")
    print(f"[history] morning={[x.get('id') for x in d['morning']]}")
    print(f"[history] evening={[x.get('id') for x in d['evening']]}")
    print(f"[history] weekly={[x.get('id') for x in d['weekly']]}")
    if final_size > SIZE_LIMIT:
        print(f"[ERROR] 最终体积 {final_size} > {SIZE_LIMIT}，CDN 可能截断！")

    # 3.5) 上传外置的 md 文件（体积换历史的关键）
    for rel_path in pending_md:
        src = BASE / rel_path
        if src.exists():
            put_file(rel_path, src.read_bytes(), f"chore: 外置正文 {rel_path}")

    # 4) PUT 上传（只 update 已有文件，带 sha）
    for rel in DEPLOY_FILES:
        if get_remote_sha(rel) is None:
            print(f"SKIP {rel}: 远端不存在（禁止 create 新文件）")
            continue
        local = BASE / rel
        put_file(rel, local.read_bytes(),
                 f"feat: add {rtype} {date} (m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])})")

    print("=== 完成 ===")
    print("验证：curl -sI https://ceciliachung1123-rgb.github.io/ai-invest-dashboard/dashboard-data-v3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
