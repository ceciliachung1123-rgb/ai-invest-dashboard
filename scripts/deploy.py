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
6. 结构化字段外置：精简最老条目时把被丢弃的字段存到 data/structured/{id}.json，
   条目里留 structured_file 引用，前端按需 fetch 回来合并（不丢数据、不涨 v3 体积）
"""
import base64
import copy
import json
import os
import re
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

# ============ 结构化字段外置（2026-11-04） ============
# 背景：compact_oldest 精简最老条目时，indices / capital_flow / hotspots /
#      key_attribution / 连板梯队 等结构化字段会被折叠成占位符（"（5 项，详细内容见正文 md）"），
#      **等于直接丢弃** —— 线上看板点开历史卡片时结构化面板是空的。
# 方案：被丢弃的字段写到 data/structured/{id}.json，条目里加 structured_file 引用，
#      前端渲染时按需 fetch 回来合并。体积换历史，和 content_md_file 同一套路。
# ------------------------------------------------------------
STRUCTURED_DIR = "structured"        # 相对 data/ 目录（与 morning-content 同级）
STRUCTURED_MAX_BYTES = 95000         # CDN 对 >100KB 单文件会截断响应，留 5% 余量
# 正文 markdown 字段：正文已在 content_md_file 里，不重复进 structured 文件
BODY_TEXT_FIELDS = ("summary_markdown", "content_md", "content")
# compact_oldest 折叠长数组时生成的占位符（用于识别「已被裁剪」的字段）
TRIM_PLACEHOLDER_RE = re.compile(r"^（\d+ 项，详细内容见正文 md）$")


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


def load_archive():
    """拉取全量历史归档 data/archive.json（不存在则返回空骨架）"""
    content, _ = pull_remote("data/archive.json")
    if content is None:
        print("[archive] 远端 archive.json 不存在，将新建")
        return {"morning": [], "evening": [], "weekly": []}
    try:
        return json.loads(content.decode("utf-8"))
    except Exception as e:
        print(f"[archive] WARN 解析失败({e})，将新建")
        return {"morning": [], "evening": [], "weekly": []}


def get_remote_sha(rel):
    req = urllib.request.Request(f"{API}/contents/{rel}", headers=HDRS)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def list_remote_structured():
    """列出远端 data/structured/ 下已有的 json 文件名（只发一次 API 请求）。

    用途：远端已有 structured 文件的条目，v3 里应该带 structured_file 引用。
    v3.json 每次部署都会被重写，引用可能丢；这里扫一遍目录把引用补回来（幂等自愈）。"""
    req = urllib.request.Request(f"{API}/contents/data/{STRUCTURED_DIR}", headers=HDRS)
    try:
        with urllib.request.urlopen(req) as resp:
            items = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return set()
        raise
    return {x.get("name") for x in items
            if x.get("type") == "file" and str(x.get("name", "")).endswith(".json")}


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
    """把超长的 markdown 文本字段外置到 md 文件，只在 v3 里留引用 + 摘要。

    返回 (状态, 相对路径) 或 None：
      ("created",   fname)  新建 md 并写入 content_md_file
      ("recreated", fname)  条目已有 content_md_file 但远端文件缺失 → 用条目正文重建（自愈）
      ("dangling",  fname)  条目已有 content_md_file、远端缺失、且条目里没有正文可重建 → 悬空
      None                 无需处理

    ⚠️ 2026-11-02 事故：旧逻辑条件含 `not item.get("content_md_file")`，
    子会话手填了 content_md_file → 函数直接 return False → **md 文件从未生成、从未 PUT**
    → v3.json 里有引用但 git 上没文件 → 看板拉正文 404。
    修复：不再盲信 content_md_file，改为**校验远端文件是否真的存在**，
    缺失就用条目正文重建（幂等自愈）。"""
    body_key = None
    for k in ("summary_markdown", "content_md", "content"):
        v = item.get(k)
        if isinstance(v, str) and len(v) > 1200:
            body_key = k
            break

    existing_ref = item.get("content_md_file")
    if not existing_ref:
        if not body_key:
            return None
        fname = f"{prefix}/{item.get('id', 'x').replace('/', '_')}.md"
        target = content_dir / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item[body_key], encoding="utf-8")
        item["content_md_file"] = fname
        item[body_key] = item[body_key][:300] + "\n\n（正文已外置，见 content_md_file）"
        return ("created", fname)

    # 条目已填 content_md_file → 三种情况
    #   a) 远端已有 → 什么都不用做
    #   b) 远端没有、**本地 content_dir 里已有该文件** → 上传它（最常见：子会话已 Write 了 md 文件）
    #   c) 远端没有、本地也没有 → 条目里有长正文则重建，否则报 dangling
    if get_remote_sha(f"data/{existing_ref}") is not None:
        return None  # 远端已有，正常

    local_target = content_dir / existing_ref
    if local_target.exists():
        # 情形 b：本地有 md 文件但远端没有 → 直接上传（不截断条目正文）
        return ("local", existing_ref)

    if body_key:
        # 情形 c-1：自愈：用条目正文重建
        local_target.parent.mkdir(parents=True, exist_ok=True)
        local_target.write_text(item[body_key], encoding="utf-8")
        item[body_key] = item[body_key][:300] + "\n\n（正文已外置，见 content_md_file）"
        return ("recreated", existing_ref)
    return ("dangling", existing_ref)


def compact_oldest(item):
    """精简最老条目：保留核心字段，长字符串截断，长数组/字典折叠。
    正文已在 content_md_file 里，结构化字段是重复信息，可以安全精简。

    返回 (精简后条目, 被精简掉的字段字典)：
      被精简掉的字段**不能直接丢** —— 它们要外置到 data/structured/{id}.json，
      条目里留 structured_file 引用，前端按需 fetch 回来合并。
      否则历史卡片上的结构化面板会变空（11-02 早/晚报事故）。"""
    out = {}
    removed = {}
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
        # 字段被改写 → 记下原值（正文 markdown 除外：正文已在 md 文件里）
        if out[k] != v and k not in BODY_TEXT_FIELDS:
            removed[k] = v
    return out, removed


# ============ 结构化字段外置相关工具 ============

def structured_ref(item_id):
    """structured 文件的引用路径（相对 data/ 目录，与 content_md_file 同级约定）"""
    safe = str(item_id or "x").replace("/", "_")
    return f"{STRUCTURED_DIR}/{safe}.json"


def json_size(obj):
    """compact 序列化后的字节数"""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def cap_structured_payload(payload, limit=STRUCTURED_MAX_BYTES):
    """保证单个 structured 文件 < limit 字节（CDN 对 >100KB 单文件会截断响应）。
    超限时优先丢最大的字段；只剩一个字段还超限就截断它。"""
    if json_size(payload) <= limit:
        return payload
    while len(payload) > 1 and json_size(payload) > limit:
        biggest = max(payload, key=lambda k: json_size({k: payload[k]}))
        payload.pop(biggest)
    if len(payload) == 1 and json_size(payload) > limit:
        k = next(iter(payload))
        v = payload[k]
        if isinstance(v, list):
            while len(v) > 1 and json_size(payload) > limit:
                v = v[:max(1, len(v) // 2)]
                payload[k] = v
        elif isinstance(v, dict):
            keys = list(v)
            while len(keys) > 1 and json_size(payload) > limit:
                keys = keys[:max(1, len(keys) // 2)]
                payload[k] = {kk: v[kk] for kk in keys}
        elif isinstance(v, str):
            payload[k] = v[:1000]
    return payload


def is_trimmed_placeholder(v):
    """compact_oldest 折叠长数组生成的占位符"""
    return isinstance(v, str) and bool(TRIM_PLACEHOLDER_RE.match(v))


def is_trimmed_dict(original, trimmed):
    """compact_oldest 对大 dict 只保留前 3 个 key —— 判断 trimmed 是否是被裁过的 dict"""
    if not (isinstance(original, dict) and isinstance(trimmed, dict)):
        return False
    if len(original) <= len(trimmed):
        return False
    if not set(trimmed.keys()).issubset(original.keys()):
        return False
    return all(trimmed[k] == original[k] for k in trimmed)


def is_trimmed_str(original, trimmed):
    """compact_oldest 对长字符串截断成 200 字 + 省略号"""
    if not (isinstance(original, str) and isinstance(trimmed, str)):
        return False
    return len(original) > 200 and trimmed == original[:200] + "…"


def is_trimmed_of(original, trimmed):
    """trimmed 是不是 original 被 compact_oldest 裁剪后的产物"""
    if original == trimmed:
        return False
    if is_trimmed_placeholder(trimmed):
        return True
    if is_trimmed_dict(original, trimmed):
        return True
    if is_trimmed_str(original, trimmed):
        return True
    return False


def merge_richer(old_item, new_item):
    """archive 合并策略：**占位符不覆盖富字段**。
    v3.json 里的条目可能已经被裁剪成占位符，如果直接写回 archive，
    archive 里保存的完整版就被占位符冲掉了（历史结构化数据永久丢失）。
    返回 (合并后的条目, 是否发生了变化)。"""
    merged = dict(new_item)
    changed = False
    for k, ov in (old_item or {}).items():
        if k not in merged:
            merged[k] = ov
            changed = True
            continue
        nv = merged[k]
        if is_trimmed_of(ov, nv):
            # 新值是被裁剪的占位符 → 保留 archive 里的完整版
            merged[k] = ov
            changed = True
    return merged, changed


def recover_from_archive(item, archive_item):
    """从 archive.json 里找回被 compact_oldest 裁掉的结构化字段。

    返回 {字段名: 完整原值}（没有可恢复的字段则返回空 dict）。
    ⚠️ 只在「v3 里的值确实是占位符、archive 里的值更完整」时才恢复，
       且要求 id/date/title 对得上（同一代报告），避免把旧版数据混进新版。"""
    if not archive_item:
        return {}
    if archive_item.get("date") != item.get("date"):
        return {}
    at, it = archive_item.get("title"), item.get("title")
    if at and it and at != it:
        return {}
    recovered = {}
    for k, av in archive_item.items():
        if k in KEEP_FIELDS or k in BODY_TEXT_FIELDS:
            continue
        if k not in item:
            continue
        if is_trimmed_of(av, item[k]):
            recovered[k] = av
    return recovered


def fit_size(dd, content_dir, archive=None, remote_structured=None):
    """把 dd 压到 ≤ SIZE_LIMIT。优先级：外置 > 精简最老 > 减条目（保底 3+3+2）。
    返回 (最终数据, 说明文本, 需要上传的 md 文件列表, 悬空引用列表, 结构化载荷字典, 被精简条目 id 列表)

    ⚠️ 外置只对「当前档位保留的条目」做。若对全量 dd 做，会生成 60+ 个
       永远用不上的 md 文件，每次部署多打 60 次 PUT，纯浪费。

    结构化载荷：{条目 id: {字段名: 完整原值}} —— 被精简掉的字段不丢，
       写盘成 data/structured/{id}.json，条目里加 structured_file 引用。
    archive：可选，data/archive.json 的内容 —— 用来找回「上一轮已经被裁掉」的字段。
    remote_structured：可选，远端 data/structured/ 已有的文件名集合 —— 用来补引用。"""
    pending_md = []
    dangling = []
    structured = {}          # {条目 id: {字段名: 完整原值}}
    trimmed_ids = []        # 本档位被精简过的条目 id（用于输出「哪些条目被精简了」）
    remote_structured = remote_structured or set()
    archive_index = {}
    for k in ("morning", "evening", "weekly"):
        for x in (archive or {}).get(k, []):
            if x.get("id"):
                archive_index[x["id"]] = x

    def externalize_set(cand):
        """对候选集做外置，返回 (新建列表, 悬空列表)"""
        created, dang = [], []
        for key, prefix in (("morning", "morning-content"), ("evening", "evening-content"),
                            ("weekly", "weekly-content")):
            for it in cand.get(key, []):
                r = externalize_longtext(it, content_dir, prefix)
                if not r:
                    continue
                status, fname = r
                if status == "dangling":
                    dang.append((key, it.get("id"), fname))
                else:
                    # created / recreated / local —— 三种都要上传
                    # ⚠️ 必须带 "data/" 前缀：main() 里用的是 BASE / rel_path，
                    #    而 content_dir = BASE/"data"，fname 是相对 content_dir 的
                    created.append(f"data/{fname}")
        return created, dang

    def recover_set(cand):
        """从 archive.json 找回被裁剪掉的结构化字段（不改动 v3 里的占位符，
        只把完整值放进 structured 载荷 → 前端 fetch 回来合并）。
        占位符留在 v3 里，体积不涨。"""
        n = 0
        for key in ("morning", "evening", "weekly"):
            for it in cand.get(key, []):
                aid = it.get("id")
                if not aid:
                    continue
                rec = recover_from_archive(it, archive_index.get(aid))
                if not rec:
                    continue
                structured.setdefault(aid, {}).update(rec)
                it["structured_file"] = structured_ref(aid)
                n += 1
        return n

    def attach_existing_refs(cand):
        """远端已有 structured 文件 → 给对应条目补上 structured_file 引用（幂等自愈）。
        v3.json 每次部署都会被重写，引用可能丢；这里按远端目录把引用补回来。"""
        n = 0
        for key in ("morning", "evening", "weekly"):
            for it in cand.get(key, []):
                aid = it.get("id")
                if not aid or it.get("structured_file"):
                    continue
                name = f"{str(aid).replace('/', '_')}.json"
                if name in remote_structured:
                    it["structured_file"] = structured_ref(aid)
                    n += 1
        return n

    # 档位：weekly 一周才一条，丢一条 = 丢一周，且条目很便宜（正文已外置，约 1KB/条）
    # → weekly 档位给得比日报宽松
    for m_lim, e_lim, w_lim in [(6, 6, 6), (5, 5, 5), (5, 5, 4), (4, 4, 3), (3, 3, 2)]:
        cand = slim(dd, m_lim, e_lim, w_lim)
        structured.clear()      # 每个档位独立重来（档位之间会互相覆盖）
        trimmed_ids.clear()

        # Step 0: 从 archive 找回上一轮被裁掉的字段（不占 v3 体积）+ 补远端已有文件的引用
        recover_set(cand)
        attach_existing_refs(cand)

        # Step 1: 外置当前保留集里的长 markdown（并自愈悬空引用）
        local_md, dang = externalize_set(cand)
        pending_md = local_md
        dangling = dang

        # Step 2: 🚨 累积精简 —— 从最老开始逐条精简，每一步都测体积，达标即停。
        #   ⚠️ 2026-11-02 事故：旧逻辑「每档只精简 1 条，不够就降档」，
        #      周报正文大 → 6+6+3 放不下 → 一路降到 3+3+2 →
        #      静默丢掉 morning-2026-10-28 / evening-2026-10-27 / weekly-2026-10-16。
        #      现在先把最老的几条逐条精简到够用为止，**尽量不降档**。
        #      代价：被精简条目的结构化字段变简略（正文仍在 md 里，用户看得到完整版）。
        #      2026-11-04 起：被精简的结构化字段会外置到 data/structured/{id}.json，
        #      前端按需补全 → 结构化面板不再空白。
        max_trim = max(len(cand.get("morning", [])), len(cand.get("evening", [])))
        for step in range(0, max_trim + 1):
            if step > 0:
                for key in ("morning", "evening"):
                    arr = cand.get(key, [])
                    if step - 1 < len(arr):
                        compacted, removed = compact_oldest(arr[step - 1])
                        arr[step - 1] = compacted
                        aid = compacted.get("id")
                        if aid:
                            trimmed_ids.append(aid)
                        if removed:
                            if aid:
                                # 本轮裁掉的字段是「最新的真值」→ 覆盖 archive 恢复值
                                structured.setdefault(aid, {}).update(removed)
                                compacted["structured_file"] = structured_ref(aid)
            size = write_compact(cand, "/tmp/_fit.json")
            if size <= SIZE_LIMIT:
                trim_note = f"（已精简最老 {step} 条）" if step else "（无需精简）"
                return (cand, f"{m_lim}+{e_lim}+{w_lim} {trim_note}", pending_md, dangling,
                        dict(structured), list(trimmed_ids))

    # Step 3: 硬保底 3+3+2，再不行就再精简一次最老的两个
    cand = slim(dd, 3, 3, 2)
    structured.clear()
    trimmed_ids.clear()
    recover_set(cand)
    attach_existing_refs(cand)
    pending_md, dangling = externalize_set(cand)
    for key in ("morning", "evening"):
        arr = cand.get(key, [])
        for i in range(min(2, len(arr))):
            compacted, removed = compact_oldest(arr[i])
            arr[i] = compacted
            aid = compacted.get("id")
            if aid:
                trimmed_ids.append(aid)
            if removed:
                if aid:
                    structured.setdefault(aid, {}).update(removed)
                    compacted["structured_file"] = structured_ref(aid)
    size = write_compact(cand, "/tmp/_fit.json")
    if size > SIZE_LIMIT:
        print(f"[fit] ERROR 精简后仍 {size} > {SIZE_LIMIT}，请人工检查")
    return (cand, "3+3+2 (硬保底，最老两条已精简)", pending_md, dangling,
            dict(structured), list(trimmed_ids))



VALID_TYPES = ("morning", "evening", "weekly")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_args(argv):
    """解析命令行参数。
    支持：deploy.py morning 2026-11-02
          deploy.py 2026-11-02
          deploy.py --type=evening --date=2026-11-02
          deploy.py morning 2026-11-02 --baseline-ref <sha>
          deploy.py morning 2026-11-02 --dry-run    （只算不传，便于本地验证）

    ⚠️ 2026-10-31 彩排发现的 bug：旧逻辑靠「rtype 是否还是默认值」猜参数角色，
       会把 `morning` 当日期、把 `2026-11-02` 当类型，导致找错文件直接退出。
       现在改为按值判定：类型 ∈ {morning,evening,weekly}，日期匹配 YYYY-MM-DD。"""
    rtype = "morning"
    date = datetime.now().strftime("%Y-%m-%d")
    baseline_ref = None
    dry_run = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--baseline-ref" and i + 1 < len(argv):
            baseline_ref = argv[i + 1]
            i += 1
        elif a.startswith("--baseline-ref="):
            baseline_ref = a.split("=", 1)[1]
        elif a == "--dry-run":
            dry_run = True
        elif a.startswith("--type="):
            v = a.split("=", 1)[1]
            if v in VALID_TYPES:
                rtype = v
            else:
                print(f"WARN: 未知类型 {v}，忽略（可选 {VALID_TYPES}）")
        elif a.startswith("--date="):
            v = a.split("=", 1)[1]
            if DATE_RE.match(v):
                date = v
            else:
                print(f"WARN: 日期格式非法 {v}，应为 YYYY-MM-DD，忽略")
        elif a in VALID_TYPES:
            rtype = a
        elif DATE_RE.match(a):
            date = a
        else:
            print(f"WARN: 无法识别的参数 {a}，忽略")
        i += 1
    return rtype, date, baseline_ref, dry_run


def main():
    rtype, date, baseline_ref, dry_run = parse_args(sys.argv[1:])

    print(f"=== 部署 {rtype} {date} ===")
    if dry_run:
        print("[dry-run] 只在本地演算，**不会 PUT 任何文件到 GitHub**")
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

    # 2.5) 更新全量历史归档 data/archive.json
    # 线上 v3.json 只保留最近几条，历史都在 git。archive.json 是完整历史快照，
    # 周报必须读它（而不是被裁剪的 v3.json），否则会读到残缺数据。
    archive = load_archive()
    for k in ("morning", "evening", "weekly"):
        m = {x.get("id"): x for x in archive.get(k, []) if x.get("id")}
        # deep copy：后面的 fit_size 会外置/截断 d 里的条目，
        # archive 必须保存**未截断的完整版**
        for x in copy.deepcopy(d.get(k, [])):
            if not x.get("id"):
                continue
            # ⚠️ 占位符不覆盖富字段：v3 里的条目可能已被 compact_oldest 裁成占位符，
            #    直接写回 archive 会把 archive 里保存的完整版冲掉 → 历史结构化数据永久丢失。
            merged, _changed = merge_richer(m.get(x["id"]), x)
            m[x["id"]] = merged
        archive[k] = sorted(m.values(), key=lambda x: x.get("date", ""))
    archive["updated_at"] = d["updated_at"]
    arch_size = write_compact(archive, "data/archive.json")
    print(f"[archive] 全量历史 m={len(archive['morning'])} e={len(archive['evening'])} "
          f"w={len(archive['weekly'])} = {arch_size} bytes")

    # 3) fit_size：优先外置/精简内容，最后才减条目（硬保底 3+3+2）
    content_dir = BASE / "data"
    remote_structured = set()
    try:
        remote_structured = list_remote_structured()
        print(f"[structured] 远端 data/{STRUCTURED_DIR}/ 已有 {len(remote_structured)} 个文件")
    except Exception as e:
        print(f"[structured] WARN 列远端 structured 目录失败({e})，跳过引用补齐")
    ids_before = {x.get("id") for k in ("morning", "evening", "weekly") for x in d.get(k, []) if x.get("id")}
    d, fit_desc, pending_md, dangling, structured, trimmed_ids = fit_size(
        d, content_dir, archive, remote_structured)
    ids_after = {x.get("id") for k in ("morning", "evening", "weekly") for x in d.get(k, []) if x.get("id")}
    dropped = sorted(ids_before - ids_after)
    final_size = write_compact(d, "dashboard-data-v3.json")
    write_compact(d, "data/dashboard-data.json")
    print(f"[fit] {fit_desc} = {final_size} bytes {'✅' if final_size <= SIZE_LIMIT else '❌ 超阈值'}")
    print(f"[counts] m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])}")
    print(f"[history] morning={[x.get('id') for x in d['morning']]}")
    print(f"[history] evening={[x.get('id') for x in d['evening']]}")
    print(f"[history] weekly={[x.get('id') for x in d['weekly']]}")
    if dropped:
        print(f"[fit] ⚠️ 因体积限制裁掉 {len(dropped)} 条：{dropped}")
        print(f"[fit]    （完整历史仍在 data/archive.json，可用 archive 重建恢复）")
    else:
        print("[fit] ✅ 无条目被裁掉")
    if trimmed_ids:
        print(f"[fit] ℹ️ 字段被精简过的条目（{len(trimmed_ids)} 条）：{trimmed_ids}")
    if final_size > SIZE_LIMIT:
        print(f"[ERROR] 最终体积 {final_size} > {SIZE_LIMIT}，CDN 可能截断！")

    # 3.45) 结构化字段外置：被精简掉的字段不丢，写进 data/structured/{id}.json
    #        条目里已带 structured_file 引用，前端渲染时按需 fetch 回来合并。
    structured_dir = content_dir / STRUCTURED_DIR
    structured_dir.mkdir(parents=True, exist_ok=True)
    pending_structured = []
    for aid in sorted(structured.keys()):
        payload = structured.get(aid) or {}
        if not payload:
            continue
        payload = cap_structured_payload(dict(payload))
        fname = structured_ref(aid)                    # structured/{id}.json（相对 data/）
        size = write_compact(payload, structured_dir / Path(fname).name)
        rel_path = f"data/{fname}"
        pending_structured.append(rel_path)
        print(f"[structured] {aid}：{len(payload)} 个字段 → {rel_path}（{size} bytes）")
        print(f"[structured]     字段：{', '.join(sorted(payload.keys()))}")
    if pending_structured:
        print(f"[structured] 共 {len(pending_structured)} 条的结构化字段已外置到 data/{STRUCTURED_DIR}/")
    else:
        print("[structured] 无结构化字段需要外置（本次未触发字段精简）")

    # 3.46) structured 悬空审计：v3 里引用了但远端不存在的结构化文件 → 面板会是空的
    dangling_structured = []
    audited_s = set()
    for k in ("morning", "evening", "weekly"):
        for it in d.get(k, []):
            f = it.get("structured_file")
            if f and f not in audited_s:
                audited_s.add(f)
                if get_remote_sha(f"data/{f}") is None and f"data/{f}" not in pending_structured:
                    print(f"🚨 [structured审计] data/{f} 远端不存在（结构化面板会是空的）")
                    dangling_structured.append((k, it.get("id"), f))
    if dangling_structured:
        print(f"🚨 [structured审计] 共 {len(dangling_structured)} 处悬空引用 —— 必须补传结构化文件！")
    else:
        print(f"[structured审计] {len(audited_s)} 个 structured_file 引用全部有效 ✅")

    # 3.4) md 悬空审计：v3 里引用了但远端不存在的正文文件 → 看板拉正文会 404
    #      （2026-11-02 事故：条目手填 content_md_file，文件却从未 PUT）
    audited = set()
    for k in ("morning", "evening", "weekly"):
        for it in d.get(k, []):
            f = it.get("content_md_file")
            if f and f not in audited:
                audited.add(f)
                if get_remote_sha(f"data/{f}") is None and f"data/{f}" not in pending_md:
                    print(f"🚨 [md审计] data/{f} 远端不存在（引用它的条目正文会 404）")
                    dangling.append((k, it.get("id"), f))
    if dangling:
        print(f"🚨 [md审计] 共 {len(dangling)} 处悬空引用 —— 必须补传 md 文件后再部署！")
    else:
        print(f"[md审计] {len(audited)} 个 content_md_file 引用全部有效 ✅")

    # 3.5) 上传外置的 md 文件（体积换历史的关键）
    for rel_path in pending_md:
        src = BASE / rel_path
        if src.exists():
            if dry_run:
                print(f"  [dry-run] 跳过 PUT {rel_path}")
                continue
            put_file(rel_path, src.read_bytes(), f"chore: 外置正文 {rel_path}")

    # 3.55) 上传外置的结构化字段文件（内容一致则跳过 → 幂等，不产生无意义 commit）
    for rel_path in pending_structured:
        src = BASE / rel_path
        if not src.exists():
            continue
        if dry_run:
            print(f"  [dry-run] 跳过 PUT {rel_path}")
            continue
        data_bytes = src.read_bytes()
        remote_same, _rsha = pull_remote(rel_path)
        if remote_same == data_bytes:
            print(f"  SKIP {rel_path}: 远端内容已一致（幂等）")
            continue
        put_file(rel_path, data_bytes, f"chore: 外置结构化字段 {rel_path}")

    # 3.6) 上传全量历史归档（周报读它拿完整历史）
    arch_local = BASE / "data" / "archive.json"
    if arch_local.exists() and get_remote_sha("data/archive.json"):
        if dry_run:
            print(f"  [dry-run] 跳过 PUT data/archive.json")
        else:
            put_file("data/archive.json", arch_local.read_bytes(),
                     f"chore: 更新全量历史归档 ({rtype} {date})")

    # 4) PUT 上传（只 update 已有文件，带 sha）
    for rel in DEPLOY_FILES:
        if get_remote_sha(rel) is None:
            print(f"SKIP {rel}: 远端不存在（禁止 create 新文件）")
            continue
        local = BASE / rel
        if dry_run:
            print(f"  [dry-run] 跳过 PUT {rel}（{local.stat().st_size} bytes）")
            continue
        put_file(rel, local.read_bytes(),
                 f"feat: add {rtype} {date} (m={len(d['morning'])} e={len(d['evening'])} w={len(d['weekly'])})")

    print("=== 完成 ===")
    if dry_run:
        print("[dry-run] 以上全部为本地演算，GitHub 上没有任何改动")
    print("验证：curl -sI https://ceciliachung1123-rgb.github.io/ai-invest-dashboard/dashboard-data-v3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
