# -*- coding: utf-8 -*-
"""两轮 patch 合并：① fit_size/[history]/通知规则 ② API 拉取/禁止自写兜底"""
import re

VALID = ("morning", "evening", "weekly")

OLD_SLIM = "- slim 自动收紧：6+6+3 → 5+5+2 → 4+4+2 → 3+3+2 → 2+2+2，直到 ≤ 85000 bytes"
NEW_SLIM = """- fit_size 体积控制（2026-10-30 修复版）：优先级 = **外置长 markdown → 精简最老条目 → 最后才减条目数**
  · 外置：summary_markdown/content_md/content 字段 > 1200 字符 → 写入 data/{type}-content/{id}.md，v3 只留 content_md_file + 300 字摘要
  · 精简最老条目：只保留核心字段（正文已在 md 里，结构化字段是重复信息，安全可弃）
  · 减条目档位：6+6+6 → 5+5+5 → 5+5+4 → 4+4+3 → 3+3+2，**3+3+2 是硬保底，绝不降到 2+2+2**
    （weekly 一周才一条，丢一条 = 丢一周，且条目便宜（约 1KB），所以 weekly 档位比日报宽松）
  · 🚨 **累积精简优先**：先把最老的条目逐条精简到体积达标为止，**尽量不降档**。
    ⚠️ 旧版「每档只精简 1 条、不够就降档」导致 11-02 周报直接降到 3+3+2，静默丢了 3 条历史。已修复。
  · ⚠️ 旧版曾因「3+3+2 超 93 bytes」静默降到 2+2+2，一次砍掉 2 条历史（morning-2026-10-28 + evening-2026-10-27）。已修复"""

VERIFY_OLD = "- 脚本会打印每个 slim 档位的 bytes，**最终 size 必须 ≤ 85000**，超了要处理"
VERIFY_NEW = """- **最终 size 必须 ≤ 85000**，超了要处理
- 🚨 **必须核对脚本打印的 `[history] morning/evening/weekly` 三个列表 + `[fit]` 那几行**：
  · `[fit] 6+6+6` 表示保留了 6 条 morning + 6 条 evening + 6 条 weekly（理想）
  · `[fit] ⚠️ 因体积限制裁掉 N 条：[...]` —— **裁掉了条目，必须在通知里如实写出被裁的 id**
  · 预期至少保留最近 3 条 morning + 3 条 evening + 2 条 weekly。低于这个数就要警惕。
  · 若某个 id 意外消失（如 evening-2026-10-27），说明发生了降档 → **从 archive.json 重建恢复**（最快）：
    ```bash
    curl -sfL --max-time 60 -H "Authorization: Bearer ${{GITHUB_TOKEN}}" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/ceciliachung1123-rgb/ai-invest-dashboard/contents/data/archive.json" -o /tmp/arch_api.json \
    && python3 -c "import base64,json;m=json.load(open('/tmp/arch_api.json'));open('/tmp/archive.json','wb').write(base64.b64decode(m['content']))"
    # 再用 deploy.py 的 fit_size 从 archive 重建（记得把新建的 md 也 PUT 上去）
    ```
    兜底：`python3 scripts/deploy.py {RPTTYPE} YYYY-MM-DD --baseline-ref <commit-sha>`
- 🚨 **必须核对 `[md审计]` 那一行**：应显示「N 个 content_md_file 引用全部有效 ✅」。
  若出现「🚨 悬空引用」，说明有 content_md_file 指向的 md 文件不存在 → 看板正文会 404。
  补救：用 Write 工具把对应 .md 写到 `data/{type}-content/{id}.md`，再重跑一次 deploy.py（会自愈补传）。
  ⚠️ **用 Write 工具写 .md 时，deploy.py 跑在同一个 sandbox，能读到并自动上传**；
  但如果在别处手工重建 v3，**必须自己把 md 文件 PUT 上去**，否则又会出现悬空引用
  （11-02 手工恢复历史时踩过：引用生成了、md 忘了传）。"""

OLD_FALLBACK_1 = "**如果脚本拉不到**：自己写等价逻辑，但必须保证「从远端拉基线」+「compact 序列化」+「size ≤ 85000 自动收紧」+「PUT 带 sha」四点。"
NEW_FALLBACK_1 = """**如果脚本拉不到**：自己写等价逻辑，但必须保证：
① 从远端拉基线（绝不读 sandbox 本地）② compact 序列化（separators=(',',':')，禁 indent=2）
③ size ≤ 85000，且**超标时优先外置/精简内容，绝不靠删历史** ④ PUT 带 sha ⑤ 部署后核对 [history] 列表"""

HIST_NOTE = """- 🚨 **通知里必须写线上真实状态**：counts/size 取自 GitHub API 实读值，不要写 sandbox 本地文件的数字。
  （10-31 跳过通知误报线上「e=3 / 83306 bytes」，实际线上已是 e=4 / 67809 bytes——本地沙箱是旧基线）"""

OLD_FETCH = """## 2. 拉部署脚本并执行
```bash
cd /workspace/ai-investment-dashboard
mkdir -p scripts
curl -sL -o scripts/deploy.py https://raw.githubusercontent.com/ceciliachung1123-rgb/ai-invest-dashboard/main/scripts/deploy.py
python3 scripts/deploy.py {RPT} YYYY-MM-DD
```"""

NEW_FETCH = """## 2. 拉部署脚本并执行
🚨 **必须用 GitHub API 拉，不要用 raw.githubusercontent.com**（11-01 实测 raw 连续 10 次全部失败、返回 0 字节；api.github.com 10/10 成功、0.29s）

```bash
cd /workspace/ai-investment-dashboard && mkdir -p scripts
for i in 1 2 3; do
  curl -sfL --max-time 30 -H "Authorization: Bearer ${{GITHUB_TOKEN}}" -H "Accept: application/vnd.github+json" \\
    "https://api.github.com/repos/ceciliachung1123-rgb/ai-invest-dashboard/contents/scripts/deploy.py" -o /tmp/deploy_api.json \\
  && python3 -c "import base64,json;m=json.load(open('/tmp/deploy_api.json'));open('scripts/deploy.py','wb').write(base64.b64decode(m['content']))" \\
  && python3 -c "import ast;ast.parse(open('scripts/deploy.py').read())" && echo "脚本就绪" && break
  echo "拉取/校验失败，重试 $i"; sleep 5
done
python3 scripts/deploy.py {RPT} YYYY-MM-DD
```

**❌ 若脚本最终仍不可用：绝不自己重写部署逻辑**（会丢掉 fit_size 安全规则、archive 归档维护、--baseline-ref 恢复能力，可能重演「静默删历史」事故）。
改为：① 再重试几轮 ② 直接用 GitHub API `contents/scripts/deploy.py` 取源码到内存执行 ③ 仍失败就 notify root 交由 root 处理，**不要硬部署**。"""

OLD_FALLBACK_2 = NEW_FALLBACK_1
NEW_FALLBACK_2 = """**如果脚本确实无法获取**：❌ **不要自己重写等价逻辑**（旧版这条兜底会丢掉 fit_size 安全规则和 archive 维护，10-30 事故就是这么重演的）。
正确做法：① 再重试几轮拉取 ② 用 GitHub API `contents/scripts/deploy.py` 直接取源码在内存里 exec ③ 仍失败则 notify root 交由 root 处理。
只有 root 明确指示时，才允许手写等价逻辑，且必须保证：① 从远端拉基线（绝不读 sandbox 本地）② compact 序列化（separators=(',',':')，禁 indent=2）③ size ≤ 85000 且超标时优先外置/精简内容、绝不靠删历史 ④ PUT 带 sha ⑤ 部署后核对 [history] 列表 ⑥ 更新 data/archive.json"""

OLD_WEEKLY_SRC = """从 GitHub 远端拉 `dashboard-data-v3.json`（sandbox 本地可能是空壳，不要依赖本地），读出上周区间的 morning / evening 条目，按日期排序拼接。

用 `curl -sL https://raw.githubusercontent.com/ceciliachung1123-rgb/ai-invest-dashboard/main/dashboard-data-v3.json` 或 GitHub API `contents/dashboard-data-v3.json`。"""

NEW_WEEKLY_SRC = """🚨 **必须从全量历史归档读，不能读线上 v3.json**：
线上 `dashboard-data-v3.json` 只保留最近 3-6 条（体积控制），**读它会拿到残缺的上周数据**（例如 10-26 的早晚报已被裁掉）。

正确数据源（按优先级）：
1. GitHub API `contents/data/archive.json` —— 全量历史归档（113+ 条），deploy.py 每次部署会自动更新
2. 兜底：raw `https://raw.githubusercontent.com/.../main/data/archive.json`
3. 再兜底：扫 git 历史 `contents/dashboard-data-v3.json?ref=<sha>` 找含目标 id 的 commit

🚨 **用 GitHub API 拉，不要用 raw**（11-01 实测 raw 连续 10 次全失败、0 字节）：

```bash
curl -sfL --max-time 60 -H "Authorization: Bearer ${{GITHUB_TOKEN}}" -H "Accept: application/vnd.github+json" \\
  "https://api.github.com/repos/ceciliachung1123-rgb/ai-invest-dashboard/contents/data/archive.json" -o /tmp/arch_api.json \\
&& python3 -c "import base64,json;m=json.load(open('/tmp/arch_api.json'));open('/tmp/archive.json','wb').write(base64.b64decode(m['content']))" \\
&& python3 -c "
import json; a=json.load(open('/tmp/archive.json'))
for k in ('morning','evening'):
    for it in a.get(k,[]):
        if '2026-10-26' <= it.get('date','') <= '2026-10-30': print(k, it.get('id'))
"
```

按日期排序拼接上周区间的 morning / evening 条目。sandbox 本地不要依赖（本地只到 09-09，10 月的留在隔离沙箱里）。"""


def patch(prompt, rptype, is_weekly=False):
    p = prompt
    for old, new in [(OLD_SLIM, NEW_SLIM),
                     (VERIFY_OLD, VERIFY_NEW.replace("{RPTYPE}", rptype)),
                     (OLD_FALLBACK_1, NEW_FALLBACK_1),
                     (OLD_FETCH.replace("{RPT}", rptype), NEW_FETCH.replace("{RPT}", rptype)),
                     (OLD_FALLBACK_2, NEW_FALLBACK_2)]:
        assert old in p, f"missing: {old[:55]}"
        p = p.replace(old, new)
    marker = "\n## 5. 结束本 session"
    assert marker in p
    p = p.replace(marker, "\n" + HIST_NOTE + marker)
    if is_weekly:
        assert OLD_WEEKLY_SRC in p
        p = p.replace(OLD_WEEKLY_SRC, NEW_WEEKLY_SRC)
    # 校验
    assert "curl -sL -o scripts/deploy.py" not in p
    assert "2+2+2，直到" not in p
    assert "不要自己重写" in p
    assert "3+3+2 是硬保底" in p
    assert "[history]" in p
    return p


if __name__ == "__main__":
    import sys
    for k, rpt, w in [("morning", "morning", False), ("evening", "evening", False), ("weekly", "weekly", True)]:
        p = patch(open(f"orig_{k}.txt", encoding="utf-8").read(), rpt, w)
        open(f"new_{k}.txt", "w", encoding="utf-8").write(p)
        print(f"{k}: {len(p)} chars ✅")
