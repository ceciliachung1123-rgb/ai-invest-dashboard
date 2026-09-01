#!/usr/bin/env python3
"""
AI 投资研究看板 - 自动发布脚本

功能：
1. 合并 data/ 下所有 morning-*.json / evening-*.json 为 dashboard-data.json
2. 推送到 GitHub（如果配置了 token）
3. 推送摘要到飞书机器人（如果配置了 webhook）
4. 重新部署（如有需要）

使用：
  python3 auto_publish.py

环境变量 / Secret：
  GITHUB_TOKEN        GitHub Personal Access Token (repo + workflow 权限)
  GITHUB_REPO         格式：username/repo-name
  FEISHU_WEBHOOK      飞书机器人 webhook URL
  DASHBOARD_PUBLIC_URL 已部署的看板公开 URL（用于飞书卡片链接）
"""

import json
import os
import sys
import glob
import subprocess
from datetime import datetime
from pathlib import Path
from urllib import request, error

ROOT = Path("/workspace/ai-investment-dashboard")
DATA_DIR = ROOT / "data"


def merge_dashboard() -> dict:
    """合并所有早报/晚报为 dashboard-data.json"""
    morning, evening, archive = [], [], []
    for f in sorted(glob.glob(str(DATA_DIR / "morning-*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        morning.extend(d.get("morning", []))
    for f in sorted(glob.glob(str(DATA_DIR / "evening-*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        evening.extend(d.get("evening", []))

    # 按日期倒序
    morning.sort(key=lambda x: x.get("date", ""), reverse=True)
    evening.sort(key=lambda x: x.get("date", ""), reverse=True)

    return {
        "morning": morning,
        "evening": evening,
        "archive": archive,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_deploy_target() -> Path:
    """返回单一固定部署目标文件

    经验教训：v4/v5/...版本号后缀的新 file 会触发 GitHub Pages
    "Page build failed" 错误（2026-09-02 验证）。只 update 已有 file
    是最稳的方式。
    """
    return ROOT / "dashboard-data-v3.json"


def write_dashboard(data: dict) -> Path:
    """写入 dashboard-data.json + 同步到部署目标"""
    out_main = DATA_DIR / "dashboard-data.json"
    out_deploy = get_deploy_target()
    with open(out_main, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(out_deploy, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[merge] wrote {out_main.name} + {out_deploy.name}")
    return out_deploy  # 返回部署目标


def push_to_github(message: str, deploy_file: Path = None) -> bool:
    """推送到 GitHub（如果配置）

    部署策略（2026-09-02 经验）：
    1. update 已有的 dashboard-data-v3.json（不要 create 新 file）
    2. 同时 update data/dashboard-data.json 作为历史归档
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        print("[github] GITHUB_TOKEN / GITHUB_REPO 未配置，跳过 push")
        return False

    if deploy_file is None:
        deploy_file = get_deploy_target()

    try:
        import base64
        from urllib import request as urlreq

        api_base = f"https://api.github.com/repos/{repo}"

        for file_path in [DATA_DIR / "dashboard-data.json", deploy_file]:
            rel_path = str(file_path.relative_to(ROOT))
            api_url = f"{api_base}/contents/{rel_path}"

            # 取 SHA
            req = urlreq.Request(
                api_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            sha = None
            try:
                with urlreq.urlopen(req) as resp:
                    data = json.loads(resp.read())
                    sha = data.get("sha")
            except error.HTTPError as e:
                if e.code != 404:
                    raise

            content_b64 = base64.b64encode(file_path.read_bytes()).decode()
            body = {"message": f"🤖 {message} ({file_path.name})", "content": content_b64}
            if sha:
                body["sha"] = sha

            put_req = urlreq.Request(
                api_url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urlreq.urlopen(put_req) as resp:
                r = json.loads(resp.read())
                print(f"[github] PUT {rel_path}: {r['commit']['sha'][:10]}")

        return True
    except Exception as e:
        print(f"[github] push 失败: {e}")
        return False


def push_to_feishu(report_type: str, content: str, date: str) -> bool:
    """推送摘要到飞书机器人（如果配置）"""
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("[feishu] FEISHU_WEBHOOK 未配置，跳过推送")
        return False

    # 提取关键信息
    title = f"📊 {date} {'早报' if report_type == 'morning' else '晚报'}"
    dashboard_url = os.environ.get("DASHBOARD_PUBLIC_URL", "")

    # 飞书消息卡片
    summary = content[:500]  # 截取前 500 字
    if len(content) > 500:
        summary += "..."

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue" if report_type == "morning" else "orange",
                "title": {"content": title, "tag": "plain_text"}
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": summary
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"content": "查看完整看板", "tag": "plain_text"},
                            "type": "primary",
                            "url": dashboard_url if dashboard_url else "https://github.com"
                        }
                    ] if dashboard_url else []
                }
            ]
        }
    }

    try:
        req = request.Request(
            webhook,
            data=json.dumps(card).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                print(f"[feishu] 推送成功: {title}")
                return True
            else:
                print(f"[feishu] 推送失败: {result}")
                return False
    except (error.URLError, error.HTTPError) as e:
        print(f"[feishu] 网络错误: {e}")
        return False


def main():
    report_type = sys.argv[1] if len(sys.argv) > 1 else "morning"
    date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")

    print(f"=== AI 看板自动发布：{report_type} {date} ===")

    # 1. 合并 + 写入
    data = merge_dashboard()
    versioned_out = write_dashboard(data)
    print(f"[merge] morning={len(data['morning'])}, evening={len(data['evening'])}")

    # 2. 找最新的报告
    target = data["morning" if report_type == "morning" else "evening"]
    if not target:
        print(f"[error] 没有 {report_type} 数据")
        return 1
    latest = target[0]
    content = latest.get("content", "")

    # 3. 推送到 GitHub（同时 PUT 版本号文件 + 主文件）
    push_msg = f"🤖 自动更新：{date} {report_type} [{datetime.now().strftime('%H:%M')}]"
    push_to_github(push_msg, deploy_file=versioned_out)

    # 4. 推送到飞书
    push_to_feishu(report_type, content, date)

    print("=== 完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
