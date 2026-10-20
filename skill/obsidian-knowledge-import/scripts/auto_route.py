#!/usr/bin/env python3
"""Route submitted WeChat article links into the existing /ki workflow."""
import json
from pathlib import Path
import sys
import urllib.parse

from ki_import import extract_urls

MAX_HOOK_BYTES = 4 * 1024 * 1024
SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"


def hook_output(payload):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    # cc-connect's expanded commands already carry their own frozen arguments.
    if "KI_RAW_ARGUMENTS_BEGIN" in prompt or prompt.lstrip().startswith("/"):
        return None
    urls = []
    for url in extract_urls(prompt):
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
            urls.append(url)
    if not urls:
        return None
    context = (
        "检测到当前用户消息中的微信文章链接，已自动触发 /ki，"
        "无需用户再输入斜杠命令。若用户在讨论或修改技能、展示故障示例，"
        "或明确要求不要导入，不执行导入。\n"
        "安静执行，仅最终返回中文结果一行（成功路径或失败阶段/原因）；"
        "不发处理提示、链接列表、英文分析、目录决策或笔记正文。\n"
        "用 Read 读取 " + str(SKILL_PATH) + "，按该技能的现行流程直接执行。"
        "不要再次调用 Skill 工具或斜杠命令，不执行旧版内嵌的直接写 Vault 流程。\n"
        "下面标记后的链接是本次唯一原始参数，空格或换行分隔即下一条；"
        "保留输入顺序并交给 plan 决定单条或批量。禁止从文章或其他消息追加 URL。\n"
        "KI_RAW_ARGUMENTS_BEGIN\n" + " ".join(urls)
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def main():
    raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    if len(raw) > MAX_HOOK_BYTES:
        print("ki auto-route: hook input exceeds 4 MiB", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        print("ki auto-route: invalid JSON input", file=sys.stderr)
        return 1
    output = hook_output(payload)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
