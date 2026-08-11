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


def write_dashboard(data: dict) -> Path:
    """写入 dashboard-data.json"""
    out = DATA_DIR / "dashboard-data.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out


def push_to_github(message: str) -> bool:
    """推送到 GitHub（如果配置）"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        print("[github] GITHUB_TOKEN / GITHUB_REPO 未配置，跳过 push")
        return False

    try:
        # 简单做法：用 git 命令行（前提是已经 git init + 配置 remote）
        # 如果用 API，需要更多代码
        subprocess.run(["git", "add", "dashboard-data.json"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, check=True, capture_output=True)
        print(f"[github] push 成功: {message}")
        return True
    except subprocess.CalledProcessError as e:
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
    out = write_dashboard(data)
    print(f"[merge] 写入 {out}")
    print(f"[merge] morning={len(data['morning'])}, evening={len(data['evening'])}")

    # 2. 找最新的报告
    target = data["morning" if report_type == "morning" else "evening"]
    if not target:
        print(f"[error] 没有 {report_type} 数据")
        return 1
    latest = target[0]
    content = latest.get("content", "")

    # 3. 推送到 GitHub
    push_msg = f"🤖 自动更新：{date} {report_type} [{datetime.now().strftime('%H:%M')}]"
    push_to_github(push_msg)

    # 4. 推送到飞书
    push_to_feishu(report_type, content, date)

    print("=== 完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
