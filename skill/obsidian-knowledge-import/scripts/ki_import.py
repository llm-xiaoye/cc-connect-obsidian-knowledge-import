#!/usr/bin/env python3
"""Deterministic helpers for the /ki Obsidian knowledge-import workflow."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MIN_CONTENT_LENGTH = 200
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 10


def configured_vault() -> str:
    if os.environ.get("KI_VAULT"):
        return os.environ["KI_VAULT"]
    config_path = Path.cwd() / ".claude" / "ki-config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KiError(f"无法读取 .claude/ki-config.json：{exc}") from exc
        vault = config.get("vault") if isinstance(config, dict) else None
        if not isinstance(vault, str) or not vault.strip():
            raise KiError(".claude/ki-config.json 缺少非空 vault 字段")
        return vault
    return "~/Documents/obsidian"
BLOCKED_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3", ".zip", ".pdf"
)
URL_RE = re.compile(r'''https?://[^\s<>"']+''')
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOG_HEADER = [
    "# 导入日志",
    "",
    "| 时间 | 来源 URL | 写入路径 | 操作 |",
    "| --- | --- | --- | --- |",
]
INDEX_TABLE = [
    "| 文档 | type | 关键词 | 核心洞察 | 日期 |",
    "|------|------|--------|----------|------|",
]
REQUIRED_BODY_SECTIONS = (
    "## 摘要",
    "## 核心要点",
    "## 关键概念",
    "## 技术细节",
    "## 实践建议",
    "## 局限性",
    "## Claude点评",
    "## 关联知识",
)


class KiError(Exception):
    """A user-facing deterministic workflow error."""


def emit(payload: Dict[str, Any], output: Optional[str] = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output:
        atomic_write(Path(output), text, 0o600)
    sys.stdout.write(text)


def read_json(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KiError(f"无法读取 JSON：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KiError(f"JSON 顶层必须是对象：{path}")
    return value


def unique_in_order(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


def validate_url(raw: str) -> Tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False, "URL 不完整"
    if parsed.scheme not in ("http", "https") or not host:
        return False, "URL 不完整"
    if parsed.username or parsed.password:
        return False, "URL 不允许包含用户凭据"
    if port is not None and not (1 <= port <= 65535):
        return False, "URL 端口无效"
    path = parsed.path.rstrip("/")
    if host == "mmbiz.qpic.cn":
        return False, "微信 CDN 图片"
    if host == "mp.weixin.qq.com" and path == "/s":
        return False, "微信公众号文章路径不完整"
    if parsed.path.lower().endswith(BLOCKED_EXTENSIONS):
        return False, "非网页内容"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return False, "URL 含控制字符"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, "URL 必须指向公开网络地址"
    try:
        if not ipaddress.ip_address(host).is_global:
            return False, "URL 必须指向公开网络地址"
    except ValueError:
        pass
    return True, ""


def public_addresses(url: str, allow_private: bool = False) -> List[str]:
    ok, reason = validate_url(url)
    if not ok and not (allow_private and reason == "URL 必须指向公开网络地址"):
        raise OSError(f"URL 拒绝访问：{reason}")
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"域名解析失败：{host}: {exc}") from exc
    if not addresses:
        raise OSError(f"域名没有可用地址：{host}")
    result: List[str] = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        if not allow_private and not ip.is_global:
            raise OSError(f"URL 解析到非公开地址：{host} -> {ip}")
        result.append(str(ip))
    return unique_in_order(result)


def assert_public_url(url: str, allow_private: bool = False) -> None:
    public_addresses(url, allow_private)


def plan_from_raw(raw: str) -> Dict[str, Any]:
    stripped = raw.strip()
    rejected: List[Dict[str, str]] = []
    single = False
    force = False

    if stripped == "--force" or stripped.startswith("--force "):
        force = True
        single = True
        value = stripped[len("--force"):].strip()
        urls = URL_RE.findall(value)
        if len(urls) != 1 or value != urls[0]:
            return {
                "status": "error",
                "code": "single-arguments-invalid",
                "message": "❌ 单条模式参数异常",
            }
        ok, reason = validate_url(urls[0])
        if not ok:
            return {
                "status": "error",
                "code": "single-arguments-invalid",
                "message": "❌ 单条模式参数异常",
                "rejected": [{"url": urls[0], "reason": reason}],
            }
        accepted = [urls[0]]
    elif stripped == "--single" or stripped.startswith("--single "):
        single = True
        value = stripped[len("--single"):].strip()
        urls = URL_RE.findall(value)
        if len(urls) != 1 or value != urls[0]:
            return {
                "status": "error",
                "code": "single-arguments-invalid",
                "message": "❌ 单条模式参数异常",
            }
        ok, reason = validate_url(urls[0])
        if not ok:
            return {
                "status": "error",
                "code": "single-arguments-invalid",
                "message": "❌ 单条模式参数异常",
                "rejected": [{"url": urls[0], "reason": reason}],
            }
        accepted = [urls[0]]
    else:
        raw_urls = unique_in_order(URL_RE.findall(raw))
        accepted = []
        for url in raw_urls:
            ok, reason = validate_url(url)
            if ok:
                accepted.append(url)
            else:
                rejected.append({"url": url, "reason": reason})
        if not accepted:
            return {
                "status": "error",
                "code": "no-valid-url",
                "message": "❌ 未检测到有效 URL",
                "rejected": rejected,
            }

    mode = "single" if single or len(accepted) == 1 else "batch"
    result: Dict[str, Any] = {
        "status": "ok",
        "mode": mode,
        "input_urls": accepted,
        "rejected": rejected,
    }
    if mode == "single":
        result["current_url"] = accepted[0]
        result["force"] = force
    return result


def command_plan(args: argparse.Namespace) -> None:
    raw = Path(args.input_file).read_text(encoding="utf-8")
    emit(plan_from_raw(raw), args.output)


def command_schedule(args: argparse.Namespace) -> None:
    plan = read_json(args.plan_file)
    if plan.get("status") != "ok" or plan.get("mode") != "batch":
        raise KiError("schedule 只接受成功的批量 plan")
    urls = plan.get("input_urls")
    if not isinstance(urls, list) or len(urls) < 2:
        raise KiError("批量 plan 至少需要两个 URL")

    binary = args.cc_connect or shutil.which("cc-connect")
    if not binary:
        raise KiError("找不到 cc-connect 可执行文件")
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    created_timer_ids: List[str] = []
    for url in urls:
        if not isinstance(url, str) or not validate_url(url)[0]:
            raise KiError("plan 中包含无效 URL")
        command = [
            binary,
            "timer", "add",
            "--delay", "10s",
            "--session-mode", "new-per-run",
            "--prompt", f"/ki --single {url}",
            "--desc", "ki-batch",
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=args.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            completed = subprocess.CompletedProcess(command, 124, "", str(exc))
        entry = {
            "url": url,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
        timer_match = re.search(r"(?m)^Timer created:\s*(\S+)\s*$", completed.stdout)
        if completed.returncode == 0 and timer_match:
            entry["timer_id"] = timer_match.group(1)
            created_timer_ids.append(timer_match.group(1))
        results.append(entry)
        if completed.returncode != 0:
            failures.append(entry)
    payload = {
        "status": "error" if failures else "ok",
        "scheduled": len(results) - len(failures),
        "total": len(results),
        "results": results,
    }
    if failures and created_timer_ids:
        rollback_results = []
        for timer_id in reversed(created_timer_ids):
            deleted = subprocess.run(
                [binary, "timer", "del", timer_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=args.timeout,
            )
            rollback_results.append({
                "timer_id": timer_id,
                "returncode": deleted.returncode,
                "stdout": deleted.stdout.strip(),
                "stderr": deleted.stderr.strip(),
            })
        payload["rollback"] = rollback_results
        payload["rolled_back"] = sum(item["returncode"] == 0 for item in rollback_results)
    if not failures:
        shown = [url if len(url) <= 60 else url[:57] + "..." for url in urls]
        payload["message"] = "已排队 " + str(len(urls)) + " 条，依次处理：\n" + "\n".join(
            f"{index}. {url}" for index, url in enumerate(shown, 1)
        )
    emit(payload, args.output)
    if failures:
        raise KiError(f"{len(failures)} 条 timer 创建失败")


def vault_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise KiError(f"Vault 不存在或不是目录：{path}")
    return path


def parse_markdown_row(line: str, expected: int) -> Optional[List[str]]:
    if not line.startswith("|") or not line.rstrip().endswith("|"):
        return None
    cells: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line.strip()[1:-1]:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells if len(cells) == expected else None


def markdown_unescape(value: str) -> str:
    return value.replace(r"\|", "|").replace(r"\\", "\\")


def find_log_url(log_path: Path, url: str) -> Optional[str]:
    if not log_path.exists():
        return None
    for line in log_path.read_text(encoding="utf-8").splitlines()[4:]:
        cells = parse_markdown_row(line, 4)
        if not cells or markdown_unescape(cells[1]) != url:
            continue
        match = re.search(r"\]\(([^)]+)\)", cells[2])
        return match.group(1) if match else ""
    return None


def command_lookup(args: argparse.Namespace) -> None:
    plan = read_json(args.plan_file)
    if plan.get("status") != "ok" or plan.get("mode") != "single":
        raise KiError("lookup 只接受成功的单条 plan")
    url = str(plan.get("current_url", ""))
    vault = vault_path(args.vault)
    with vault_lock(vault):
        recovered = recover_journal(vault)
        found = find_log_url(vault / "_import-log.md", url)
    if found is None:
        emit({
            "status": "ok",
            "duplicate": False,
            "current_url": url,
            "recovered_previous_transaction": recovered,
        }, args.output)
    elif plan.get("force") is True:
        emit({
            "status": "ok",
            "duplicate": True,
            "bypass": True,
            "current_url": url,
            "path": found,
            "recovered_previous_transaction": recovered,
            "message": f"已确认更新：继续处理 {found}",
        }, args.output)
    else:
        emit({
            "status": "ok",
            "duplicate": True,
            "current_url": url,
            "path": found,
            "recovered_previous_transaction": recovered,
            "message": f"⚠️ 已导入，路径：{found}。回复「更新」强制覆盖。",
        }, args.output)


def request_text(url: str, timeout: int, allow_private: bool = False) -> Tuple[str, str]:
    """Fetch through the pinned-IP curl implementation."""
    return curl_text(url, timeout, allow_private=allow_private)


def curl_text(url: str, timeout: int, allow_private: bool = False) -> Tuple[str, str]:
    """Fetch with curl argv, validating each redirect before following it."""
    curl = shutil.which("curl") or "/usr/bin/curl"
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        addresses = public_addresses(current, allow_private)
        parsed = urllib.parse.urlparse(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with tempfile.TemporaryDirectory(prefix="ki-curl-") as directory:
            header_path = Path(directory) / "headers"
            body_path = Path(directory) / "body"
            command = [
                curl,
                "-sS",
                "--compressed",
                "--max-time",
                str(timeout),
                "--connect-timeout",
                str(min(timeout, 10)),
                "--max-filesize",
                str(MAX_RESPONSE_BYTES),
                "--noproxy",
                "*",
                "--dump-header",
                str(header_path),
                "--output",
                str(body_path),
                "-A",
                USER_AGENT,
            ]
            result = None
            errors = []
            for address in addresses:
                resolved_address = f"[{address}]" if ":" in address else address
                pinned = [
                    *command,
                    "--resolve",
                    f"{host}:{port}:{resolved_address}",
                    current,
                ]
                try:
                    attempt = subprocess.run(
                        pinned,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=timeout + 5,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(str(exc))
                    continue
                result = attempt
                if attempt.returncode == 0:
                    break
                errors.append(attempt.stderr.decode("utf-8", errors="replace").strip())
            if result is None:
                raise OSError("curl 执行失败：" + "; ".join(errors)[:500])
            if result.returncode != 0:
                raise OSError(f"curl 退出码 {result.returncode}：{'; '.join(errors)[:500]}")
            header_text = header_path.read_text(encoding="iso-8859-1") if header_path.exists() else ""
            blocks = [block for block in re.split(r"\r?\n\r?\n", header_text) if block.startswith("HTTP/")]
            last_header = blocks[-1] if blocks else ""
            status_match = re.match(r"HTTP/\S+\s+(\d{3})", last_header)
            status = int(status_match.group(1)) if status_match else 0
            location_match = re.search(r"(?mi)^Location:\s*(.+?)\s*$", last_header)
            if status in (301, 302, 303, 307, 308) and location_match:
                current = urllib.parse.urljoin(current, location_match.group(1))
                continue
            raw = body_path.read_bytes() if body_path.exists() else b""
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OSError(f"响应超过 {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB 上限")
            return raw.decode("utf-8", errors="replace"), "utf-8"
    raise OSError(f"重定向超过 {MAX_REDIRECTS} 次")


def normalize_text(value: str) -> str:
    value = html.unescape(value).replace("\u200b", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_html(source: str, selector: Optional[str] = None) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(source, "html.parser")
        root = soup.select_one(selector) if selector else soup
        if root is None:
            return ""
        for node in root.select("script, style, noscript, svg"):
            node.decompose()
        return normalize_text(root.get_text("\n"))
    except ImportError:
        if selector:
            match = re.search(
                r'<[^>]+id=["\']js_content["\'][^>]*>(.*?)</[^>]+>',
                source,
                flags=re.I | re.S,
            )
            source = match.group(1) if match else ""
        source = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", source, flags=re.I | re.S)
        source = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", source, flags=re.I)
        return normalize_text(re.sub(r"<[^>]+>", " ", source))


def decode_js_string(value: str) -> str:
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)
    return html.unescape(
        value.replace(r"\/", "/").replace(r"\'", "'").replace(r'\"', '"').replace(r"\\", "\\")
    ).strip()


def parse_wechat(source: str) -> Tuple[str, str, str]:
    title = ""
    for pattern in (
        r"var\s+msg_title\s*=\s*'((?:\\.|[^'\\])*)'",
        r'var\s+msg_title\s*=\s*"((?:\\.|[^"\\])*)"',
        r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
    ):
        match = re.search(pattern, source, flags=re.I | re.S)
        if match:
            title = decode_js_string(match.group(1))
            break
    article_date = ""
    match = re.search(r"var\s+ct\s*=\s*['\"]?(\d{9,13})", source)
    if match:
        stamp = int(match.group(1))
        if stamp > 10_000_000_000:
            stamp //= 1000
        article_date = dt.datetime.fromtimestamp(stamp).astimezone().date().isoformat()
    body = strip_html(source, "#js_content")
    return title, article_date, body


def parse_generic_html(source: str) -> Tuple[str, str, str]:
    title = ""
    for pattern in (
        r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, source, flags=re.I | re.S)
        if match:
            title = normalize_text(re.sub(r"<[^>]+>", "", match.group(1)))
            break
    article_date = ""
    for pattern in (
        r'<meta\s+property=["\']article:published_time["\']\s+content=["\'](.*?)["\']',
        r'<time[^>]+datetime=["\'](.*?)["\']',
    ):
        match = re.search(pattern, source, flags=re.I | re.S)
        if match:
            date_match = re.search(r"\d{4}-\d{2}-\d{2}", match.group(1))
            if date_match:
                article_date = date_match.group(0)
                break
    return title, article_date, strip_html(source)


def parse_jina(source: str) -> Tuple[str, str, str]:
    title_match = re.search(r"(?m)^Title:\s*(.+)$", source)
    title = normalize_text(title_match.group(1)) if title_match else ""
    date_match = re.search(
        r"(?mi)^(?:Published Time|Published|Date):\s*.*?(\d{4}-\d{2}-\d{2})",
        source,
    )
    article_date = date_match.group(1) if date_match else ""
    marker = re.search(r"(?mi)^Markdown Content:\s*$", source)
    body = source[marker.end():] if marker else source
    return title, article_date, normalize_text(body)


def safe_jina_url(base: str, current_url: str) -> str:
    if "{url}" in base:
        return base.format(url=urllib.parse.quote(current_url, safe=""))
    return base.rstrip("/") + "/" + current_url


def fetch_article(
    current_url: str,
    timeout: int,
    jina_base: str,
    allow_private: bool = False,
) -> Dict[str, Any]:
    jina_title = jina_date = jina_body = ""
    jina_error = ""
    try:
        jina_source, _ = request_text(
            safe_jina_url(jina_base, current_url), timeout, allow_private=allow_private
        )
        jina_title, jina_date, jina_body = parse_jina(jina_source)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        jina_error = str(exc)

    direct_title = direct_date = direct_body = ""
    direct_error = ""
    if len(jina_body) < MIN_CONTENT_LENGTH:
        try:
            # Match the legacy /ki contract: Jina first, then curl -L on the
            # original URL. argv execution prevents shell metacharacter abuse.
            direct_source, _ = curl_text(current_url, timeout, allow_private=allow_private)
            host = (urllib.parse.urlparse(current_url).hostname or "").lower()
            if host == "mp.weixin.qq.com":
                direct_title, direct_date, direct_body = parse_wechat(direct_source)
            else:
                direct_title, direct_date, direct_body = parse_generic_html(direct_source)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            direct_error = str(exc)

    if len(jina_body) >= MIN_CONTENT_LENGTH:
        method, title, article_date, body = "jina", jina_title, jina_date, jina_body
    elif len(direct_body) >= MIN_CONTENT_LENGTH:
        method, title, article_date, body = "direct", direct_title, direct_date, direct_body
    else:
        return {
            "status": "error",
            "code": "fetch-insufficient",
            "message": (
                f"❌ 抓取失败（WebFetch: {len(jina_body)}字 / curl: {len(direct_body)}字），"
                "可能付费墙。请手动复制正文重发。"
            ),
            "jina_length": len(jina_body),
            "direct_length": len(direct_body),
            "jina_error": jina_error,
            "direct_error": direct_error,
        }
    if not title:
        title = direct_title or jina_title
    if not article_date:
        article_date = direct_date or jina_date or dt.date.today().isoformat()
    return {
        "status": "ok",
        "source_url": current_url,
        "title": title,
        "article_date": article_date,
        "article_text": body,
        "content_length": len(body),
        "method": method,
        "jina_length": len(jina_body),
        "direct_length": len(direct_body),
    }


def command_fetch(args: argparse.Namespace) -> None:
    plan = read_json(args.plan_file)
    if plan.get("status") != "ok" or plan.get("mode") != "single":
        raise KiError("fetch 只接受成功的单条 plan")
    url = str(plan.get("current_url", ""))
    ok, reason = validate_url(url)
    if not ok:
        raise KiError(f"CURRENT_URL 无效：{reason}")
    payload = fetch_article(url, args.timeout, args.jina_base)
    emit(payload, args.output)
    if payload["status"] != "ok":
        raise KiError(payload["message"])


def domain_roots(vault: Path, domain: str) -> List[Path]:
    mapping = {
        "ai": [vault / "AI", vault / "knowledge-base"],
        "tools": [vault / "工具"],
        "system": [vault / "系统"],
        "all": [vault],
    }
    return [root for root in mapping[domain] if root.exists()]


def command_scan(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    terms = unique_in_order(
        term.casefold() for term in Path(args.terms_file).read_text(encoding="utf-8").splitlines() if term.strip()
    )
    scored: List[Tuple[int, float, str, str]] = []
    for root in domain_roots(vault, args.domain):
        for path in root.rglob("*.md"):
            if path.name in ("_import-log.md", "_knowledge-index.md"):
                continue
            try:
                first_lines = "\n".join(path.read_text(encoding="utf-8").splitlines()[:60])
            except OSError:
                continue
            haystack = (path.stem + "\n" + first_lines).casefold()
            hits = sum(1 for term in terms if term and term in haystack)
            if not hits:
                continue
            density = hits / max(len(terms), 1)
            excerpt = normalize_text(first_lines)[: args.excerpt_chars]
            scored.append((hits, density, path.relative_to(vault).as_posix(), excerpt))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    emit({
        "status": "ok",
        "domain": args.domain,
        "terms": terms,
        "candidates": [
            {"path": path, "term_hits": hits, "term_density": density, "excerpt": excerpt}
            for hits, density, path, excerpt in scored[: args.limit]
        ],
    }, args.output)


def safe_relative_path(vault: Path, value: str) -> Tuple[str, Path]:
    if any(char in value for char in "\n\r|\\"):
        raise KiError("写入路径含 Markdown 表格非法字符")
    relative = Path(value)
    if relative.is_absolute() or not value or "\x00" in value:
        raise KiError("写入路径必须是 Vault 相对路径")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise KiError("写入路径包含非法片段")
    if relative.suffix.lower() != ".md":
        raise KiError("写入路径必须以 .md 结尾")
    target = vault.joinpath(relative)
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(vault.resolve())
    except ValueError as exc:
        raise KiError("写入路径越出 Vault") from exc
    if target.exists():
        try:
            target.resolve().relative_to(vault.resolve())
        except ValueError as exc:
            raise KiError("写入目标通过软链接越出 Vault") from exc
    return relative.as_posix(), target


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def validate_iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise KiError(f"{field} 必须是 YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise KiError(f"{field} 必须是 YYYY-MM-DD") from exc


def validate_tags(value: Any) -> List[str]:
    if not isinstance(value, list) or not 3 <= len(value) <= 12:
        raise KiError("tags 必须是 3-12 个字符串")
    tags: List[str] = []
    for tag in value:
        if not isinstance(tag, str) or not tag.strip() or any(ch in tag for ch in "\n\r|"):
            raise KiError("tags 包含无效值")
        tags.append(tag.strip())
    return unique_in_order(tags)


def validate_single_line(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in "\n\r"):
        raise KiError(f"{field} 必须是非空单行字符串")
    return value.strip()


def validate_body(body: str) -> str:
    body = body.strip() + "\n"
    missing = [heading for heading in REQUIRED_BODY_SECTIONS if heading not in body]
    if missing:
        raise KiError("笔记正文缺少章节：" + "、".join(missing))
    if body.startswith("---"):
        raise KiError("body_file 只应包含正文，不应包含 frontmatter")
    return body


def build_new_note(meta: Dict[str, Any], current_url: str, today: str, body: str) -> str:
    title = validate_single_line(meta["title"], "title")
    article_date = validate_iso_date(meta["article_date"], "article_date")
    tags = validate_tags(meta["tags"])
    note_type = validate_single_line(meta["type"], "type")
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        "sources:",
        f"  - {yaml_quote(current_url)}",
        f"date: {article_date}",
        f"created: {today}",
        f"updated: {today}",
        "tags:",
    ]
    lines.extend(f"  - {yaml_quote(tag)}" for tag in tags)
    lines.extend([f"type: {yaml_quote(note_type)}", "---", "", body.rstrip(), ""])
    return "\n".join(lines)


def split_frontmatter(note: str) -> Tuple[List[str], str]:
    lines = note.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KiError("追加目标缺少 YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise KiError("追加目标 frontmatter 未闭合") from exc
    return lines[1:end], "\n".join(lines[end + 1:]).strip("\n")


def field_bounds(lines: List[str], key: str) -> Optional[Tuple[int, int]]:
    start = None
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)", line)
        if match and match.group(1) == key:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:(?:\s|$)", lines[index]):
            end = index
            break
    return start, end


def parse_yaml_list(lines: List[str], key: str) -> List[str]:
    bounds = field_bounds(lines, key)
    if not bounds:
        return []
    start, end = bounds
    value = lines[start].split(":", 1)[1].strip()
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value.replace("'", '"'))
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [part.strip().strip('"\'') for part in value[1:-1].split(",") if part.strip()]
    result = []
    for line in lines[start + 1:end]:
        match = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if match:
            result.append(match.group(1).strip('"\''))
    return result


def parse_yaml_scalar(lines: List[str], key: str) -> str:
    bounds = field_bounds(lines, key)
    if not bounds:
        return ""
    value = lines[bounds[0]].split(":", 1)[1].strip()
    return value.strip('"\'')


def replace_yaml_field(lines: List[str], key: str, replacement: List[str]) -> List[str]:
    bounds = field_bounds(lines, key)
    if bounds:
        start, end = bounds
        return lines[:start] + replacement + lines[end:]
    return lines + replacement


def update_append_note(
    existing: str,
    current_url: str,
    today: str,
    new_tags: Sequence[str],
    append_content: str,
) -> Tuple[str, Dict[str, Any]]:
    front, body = split_frontmatter(existing)
    sources = unique_in_order(parse_yaml_list(front, "sources") + [current_url])
    tags = unique_in_order(parse_yaml_list(front, "tags") + list(new_tags))
    front = replace_yaml_field(front, "sources", ["sources:"] + [f"  - {yaml_quote(v)}" for v in sources])
    front = replace_yaml_field(front, "updated", [f"updated: {today}"])
    front = replace_yaml_field(front, "tags", ["tags:"] + [f"  - {yaml_quote(v)}" for v in tags])
    date_value = parse_yaml_scalar(front, "date")
    validate_iso_date(date_value, "原笔记 date")
    title = parse_yaml_scalar(front, "title")
    note_type = parse_yaml_scalar(front, "type")
    if not title or not note_type:
        raise KiError("追加目标缺少 title 或 type")
    content = append_content.strip()
    if not re.match(r"^## 补充 \d{4}-\d{2}-\d{2}(?:\s|$)", content):
        raise KiError("追加正文必须以“## 补充 YYYY-MM-DD”开头")
    note = "\n".join(["---", *front, "---", "", body, "", content, ""])
    return note, {"title": title, "date": date_value, "type": note_type, "tags": tags}


def next_available_path(vault: Path, relative: str) -> Tuple[str, Path]:
    relative, target = safe_relative_path(vault, relative)
    if not target.exists():
        return relative, target
    base = Path(relative)
    number = 2
    while True:
        candidate = base.with_name(f"{base.stem}-{number}{base.suffix}").as_posix()
        candidate_relative, candidate_target = safe_relative_path(vault, candidate)
        if not candidate_target.exists():
            return candidate_relative, candidate_target
        number += 1


def system_timestamp() -> str:
    completed = subprocess.run(
        ["/bin/date", "+%Y-%m-%d %H:%M:%S"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        raise KiError("无法从系统 date 获取时间")
    return value


def insert_log(existing: str, timestamp: str, url: str, relative: str, action: str) -> str:
    lines = existing.splitlines() if existing.strip() else LOG_HEADER.copy()
    if lines and lines[0].strip() == "# 导入日志":
        header_at = next(
            (
                i
                for i, line in enumerate(lines)
                if parse_markdown_row(line, 4) and "时间" in line and "来源 URL" in line
            ),
            None,
        )
        if header_at is not None:
            if any(line.strip() for line in lines[1:header_at]):
                raise KiError("_import-log.md 表头前包含未知内容，拒绝自动修复")
            repaired = ["# 导入日志", "", lines[header_at]]
            separator_seen = False
            separator_re = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|){4}")
            for line in lines[header_at + 1 :]:
                stripped = line.strip()
                is_separator = bool(separator_re.fullmatch(stripped))
                if not separator_seen and (not stripped or is_separator):
                    if is_separator:
                        separator_seen = True
                        repaired.append("| --- | --- | --- | --- |")
                    continue
                if separator_seen and (not stripped or is_separator):
                    continue
                repaired.append(line)
            if not separator_seen:
                repaired.insert(header_at + 1, "| --- | --- | --- | --- |")
            lines = repaired
    if len(lines) < 4 or parse_markdown_row(lines[2], 4) is None:
        raise KiError("_import-log.md 表头无效，拒绝破坏性重写")
    row = f"| {timestamp} | {markdown_escape(url)} | [{relative}]({relative}) | {action} |"
    data_indices: List[Tuple[int, str]] = []
    for index, line in enumerate(lines[4:], 4):
        if not line.strip():
            continue
        cells = parse_markdown_row(line, 4)
        if cells is None:
            raise KiError(f"_import-log.md 第 {index + 1} 行结构无效")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", cells[0]):
            raise KiError(f"_import-log.md 第 {index + 1} 行时间无效")
        data_indices.append((index, cells[0]))
    insert_at = len(lines)
    for index, old_timestamp in data_indices:
        if old_timestamp <= timestamp:
            insert_at = index
            break
    lines.insert(insert_at, row)
    return "\n".join(lines).rstrip() + "\n"


def index_row(meta: Dict[str, Any], relative: str) -> str:
    tags = " ".join(f"`{markdown_escape(tag)}`" for tag in meta["tags"])
    title = markdown_escape(str(meta["title"]))
    note_type = markdown_escape(str(meta["type"]))
    insight = markdown_escape(str(meta["insight"]))
    if len(insight) > 30:
        raise KiError("核心洞察不得超过 30 个字符")
    date_value = validate_iso_date(meta["article_date"], "article_date")
    return f"| [{title}]({relative}) | {note_type} | {tags} | {insight} | {date_value} |"


def update_index(existing: str, meta: Dict[str, Any], relative: str, today: str) -> str:
    top = str(meta["top_level"]).strip()
    second = str(meta["second_level"]).strip()
    if not top or not second or any(ch in top + second for ch in "\n\r|"):
        raise KiError("顶层和二级分类无效")
    lines = existing.splitlines() if existing.strip() else ["# 知识索引", "", f"_最后更新：{today}_"]
    update_re = re.compile(r"^_最后更新：\d{4}-\d{2}-\d{2}_$")
    for index, line in enumerate(lines):
        if update_re.match(line):
            lines[index] = f"_最后更新：{today}_"
            break
    else:
        lines.insert(1, "")
        lines.insert(2, f"_最后更新：{today}_")

    heading = f"## {top} / {second}"
    try:
        section_start = lines.index(heading)
    except ValueError:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([heading, "", *INDEX_TABLE])
        section_start = lines.index(heading)
    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = index
            break
    header_index = None
    for index in range(section_start + 1, section_end):
        if parse_markdown_row(lines[index], 5) and "文档" in lines[index] and "type" in lines[index]:
            header_index = index
            break
    if header_index is None or header_index + 1 >= len(lines) or not lines[header_index + 1].startswith("|"):
        raise KiError(f"知识索引 section 表头无效：{heading}")

    row = index_row(meta, relative)
    existing_path_pattern = re.compile(r"\]\(" + re.escape(relative) + r"\)")
    for index in range(header_index + 2, section_end):
        if existing_path_pattern.search(lines[index]):
            lines[index] = row
            break
    else:
        lines.insert(header_index + 2, row)
    return "\n".join(lines).rstrip() + "\n"


def atomic_write(path: Path, content: str, default_mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else default_mode
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def lock_path(vault: Path) -> Path:
    digest = hashlib.sha256(str(vault.resolve()).encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"ki-import-{digest}.lock"


@contextlib.contextmanager
def vault_lock(vault: Path):
    path = lock_path(vault)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def snapshot(path: Path) -> Tuple[bool, bytes, int]:
    if not path.exists():
        return False, b"", 0o644
    return True, path.read_bytes(), path.stat().st_mode & 0o777


def restore(path: Path, state: Tuple[bool, bytes, int]) -> None:
    existed, content, mode = state
    if not existed:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def journal_path(vault: Path) -> Path:
    return vault / ".ki-import-transaction.json"


def missing_parent_dirs(path: Path, vault: Path) -> List[Path]:
    missing: List[Path] = []
    parent = path.parent
    while parent != vault:
        if not parent.exists():
            missing.append(parent)
        parent = parent.parent
    return missing


def write_journal(
    vault: Path,
    states: Dict[Path, Tuple[bool, bytes, int]],
    created_dirs: Sequence[Path] = (),
) -> Path:
    payload = {
        "version": 1,
        "vault": str(vault.resolve()),
        "states": [
            {
                "relative": path.relative_to(vault).as_posix(),
                "existed": state[0],
                "content_b64": base64.b64encode(state[1]).decode("ascii"),
                "mode": state[2],
            }
            for path, state in states.items()
        ],
        "created_dirs": [path.relative_to(vault).as_posix() for path in created_dirs],
    }
    path = journal_path(vault)
    atomic_write(path, json.dumps(payload, ensure_ascii=False) + "\n", 0o600)
    return path


def remove_created_dirs(vault: Path, relatives: Sequence[str]) -> None:
    for relative in relatives:
        candidate = Path(relative)
        if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
            raise KiError("事务恢复日志包含非法目录")
        parent = vault / candidate
        try:
            parent.rmdir()
        except OSError:
            continue


def recover_journal(vault: Path) -> bool:
    path = journal_path(vault)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or payload.get("vault") != str(vault.resolve()):
            raise KiError("事务恢复日志与当前 Vault 不匹配")
        for item in reversed(payload.get("states", [])):
            relative, target = safe_relative_path(vault, item["relative"])
            state = (
                bool(item["existed"]),
                base64.b64decode(item["content_b64"], validate=True),
                int(item["mode"]),
            )
            restore(target, state)
        created_dirs = payload.get("created_dirs", [])
        if not isinstance(created_dirs, list) or not all(isinstance(item, str) for item in created_dirs):
            raise KiError("事务恢复日志 created_dirs 无效")
        remove_created_dirs(vault, created_dirs)
        path.unlink()
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise KiError(f"事务恢复失败，已停止写入：{exc}") from exc


def validate_analysis(meta: Dict[str, Any], current_url: str) -> Dict[str, Any]:
    if meta.get("current_url") != current_url:
        raise KiError("❌ 内部保护：检测到非输入 URL，已停止，未写入文件。")
    if meta.get("action") not in ("new", "append"):
        raise KiError("action 必须是 new 或 append")
    for key in ("relative_path", "insight"):
        if not isinstance(meta.get(key), str) or not str(meta[key]).strip():
            raise KiError(f"{key} 不得为空")
    meta["tags"] = validate_tags(meta.get("tags"))
    relative = Path(str(meta["relative_path"]))
    top = validate_single_line(meta.get("top_level"), "top_level")
    second = validate_single_line(meta.get("second_level"), "second_level")
    if "|" in top or "|" in second:
        raise KiError("top_level/second_level 含 Markdown 表格非法字符")
    if len(relative.parts) < 3:
        raise KiError("relative_path 至少包含顶层、二级目录和文件名")
    if relative.parts[0] != top or relative.parts[1] != second:
        raise KiError("relative_path 与 top_level/second_level 不一致")
    validate_single_line(meta.get("title"), "title")
    validate_single_line(meta.get("type"), "type")
    if len(str(meta["insight"]).strip()) > 30:
        raise KiError("核心洞察不得超过 30 个字符")
    return meta


def command_commit(args: argparse.Namespace) -> None:
    plan = read_json(args.plan_file)
    if plan.get("status") != "ok" or plan.get("mode") != "single":
        raise KiError("commit 只接受成功的单条 plan")
    current_url = str(plan.get("current_url", ""))
    meta = validate_analysis(read_json(args.analysis_file), current_url)
    vault = vault_path(args.vault)
    body = Path(args.body_file).read_text(encoding="utf-8")
    timestamp = args.timestamp or system_timestamp()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", timestamp):
        raise KiError("timestamp 格式无效")
    today = timestamp[:10]
    log_path = vault / "_import-log.md"
    index_path = vault / "_knowledge-index.md"

    with vault_lock(vault):
        recovered = recover_journal(vault)
        duplicate = find_log_url(log_path, current_url)
        force = args.force or plan.get("force") is True
        if duplicate is not None and not force:
            emit({
                "status": "duplicate",
                "path": duplicate,
                "message": f"⚠️ 已导入，路径：{duplicate}。回复「更新」强制覆盖。",
            }, args.output)
            return

        action = str(meta["action"])
        if action == "new":
            requested_relative = str(meta["relative_path"])
            if force and duplicate is not None:
                if requested_relative != duplicate:
                    raise KiError("强制覆盖路径必须与导入日志中的原路径完全一致")
                relative, note_path = safe_relative_path(vault, duplicate)
                if not note_path.is_file():
                    raise KiError(f"强制覆盖目标不存在：{relative}")
            else:
                relative, note_path = next_available_path(vault, requested_relative)
            note_content = build_new_note(meta, current_url, today, validate_body(body))
            index_meta = {
                **meta,
                "article_date": validate_iso_date(meta["article_date"], "article_date"),
            }
        else:
            relative, note_path = safe_relative_path(vault, str(meta["relative_path"]))
            if force and duplicate is not None and relative != duplicate:
                raise KiError("强制覆盖路径必须与导入日志中的原路径完全一致")
            if not note_path.exists():
                raise KiError(f"追加目标不存在：{relative}")
            note_content, original = update_append_note(
                note_path.read_text(encoding="utf-8"),
                current_url,
                today,
                meta["tags"],
                body,
            )
            parts = Path(relative).parts
            if len(parts) < 3:
                raise KiError("追加目标路径不足以确定顶层/二级分类")
            index_meta = {
                **meta,
                "title": original["title"],
                "article_date": original["date"],
                "type": original["type"],
                "tags": original["tags"],
                "top_level": parts[0],
                "second_level": parts[1],
            }

        log_existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        index_existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        log_content = insert_log(log_existing, timestamp, current_url, relative, "新建" if action == "new" else "追加")
        index_content = update_index(index_existing, index_meta, relative, today)

        if args.dry_run:
            emit({
                "status": "ok",
                "dry_run": True,
                "action": action,
                "path": relative,
                "timestamp": timestamp,
            }, args.output)
            return

        paths = [note_path, log_path, index_path]
        states = {path: snapshot(path) for path in paths}
        created_dirs = missing_parent_dirs(note_path, vault)
        transaction_journal = write_journal(vault, states, created_dirs)
        try:
            atomic_write(note_path, note_content, 0o644)
            if os.environ.get("KI_FAIL_AFTER_REPLACE") == "note":
                raise OSError("injected failure after note")
            atomic_write(log_path, log_content, 0o600)
            if os.environ.get("KI_FAIL_AFTER_REPLACE") == "log":
                raise OSError("injected failure after log")
            atomic_write(index_path, index_content, 0o600)
            if os.environ.get("KI_FAIL_AFTER_REPLACE") == "index":
                raise OSError("injected failure after index")
        except BaseException:
            for path in reversed(paths):
                restore(path, states[path])
            remove_created_dirs(vault, [path.relative_to(vault).as_posix() for path in created_dirs])
            with contextlib.suppress(FileNotFoundError):
                transaction_journal.unlink()
            raise
        transaction_journal.unlink()

        table_action = "新建" if action == "new" else "追加"
        table = (
            f"{table_action}：\n"
            f"| {timestamp[:16]} | {current_url} | [{relative}]({relative}) | {table_action} |"
        )
        emit({
            "status": "ok",
            "action": action,
            "path": relative,
            "timestamp": timestamp,
            "recovered_previous_transaction": recovered,
            "table": table,
        }, args.output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="freeze URLs from raw /ki arguments")
    plan.add_argument("--input-file", required=True)
    plan.add_argument("--output")
    plan.set_defaults(handler=command_plan)

    schedule = sub.add_parser("schedule", help="create one safe timer per batch URL")
    schedule.add_argument("--plan-file", required=True)
    schedule.add_argument("--cc-connect")
    schedule.add_argument("--timeout", type=int, default=30)
    schedule.add_argument("--output")
    schedule.set_defaults(handler=command_schedule)

    lookup = sub.add_parser("lookup", help="check exact CURRENT_URL in the import log")
    lookup.add_argument("--plan-file", required=True)
    lookup.add_argument("--vault", default=configured_vault())
    lookup.add_argument("--output")
    lookup.set_defaults(handler=command_lookup)

    fetch = sub.add_parser("fetch", help="fetch CURRENT_URL without a shell")
    fetch.add_argument("--plan-file", required=True)
    fetch.add_argument("--timeout", type=int, default=30)
    fetch.add_argument("--jina-base", default=os.environ.get("KI_JINA_BASE", "https://r.jina.ai"))
    fetch.add_argument("--output")
    fetch.set_defaults(handler=command_fetch)

    scan = sub.add_parser("scan", help="rank similarity candidates from the first 60 lines")
    scan.add_argument("--vault", default=configured_vault())
    scan.add_argument("--domain", choices=("ai", "tools", "system", "all"), required=True)
    scan.add_argument("--terms-file", required=True)
    scan.add_argument("--limit", type=int, default=20)
    scan.add_argument("--excerpt-chars", type=int, default=800)
    scan.add_argument("--output")
    scan.set_defaults(handler=command_scan)

    commit = sub.add_parser("commit", help="transactionally write note, log, and index")
    commit.add_argument("--plan-file", required=True)
    commit.add_argument("--analysis-file", required=True)
    commit.add_argument("--body-file", required=True)
    commit.add_argument("--vault", default=configured_vault())
    commit.add_argument("--timestamp", help="test-only deterministic timestamp")
    commit.add_argument("--force", action="store_true")
    commit.add_argument("--dry-run", action="store_true")
    commit.add_argument("--output")
    commit.set_defaults(handler=command_commit)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
        return 0
    except KiError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"ERROR: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
