from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import multiprocessing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/obsidian-knowledge-import/scripts/ki_import.py"
spec = importlib.util.spec_from_file_location("ki_import", SCRIPT)
ki = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ki)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def base_plan(url: str = "https://example.com/article") -> dict:
    return {
        "status": "ok",
        "mode": "single",
        "input_urls": [url],
        "rejected": [],
        "current_url": url,
    }


def new_analysis(url: str = "https://example.com/article", path: str = "AI/Agent/测试主题.md") -> dict:
    return {
        "current_url": url,
        "action": "new",
        "relative_path": path,
        "title": "测试文章标题",
        "article_date": "2026-08-01",
        "type": "实践案例",
        "tags": ["agent", "workflow", "tool-use", "实践案例"],
        "top_level": "AI",
        "second_level": "Agent",
        "insight": "确定性提交避免并发损坏",
    }


def full_body() -> str:
    return """## 摘要

这是一段用于端到端测试的完整摘要，确保笔记结构保持不变。

## 核心要点

1. 第一条要点。

## 关键概念

- **事务提交**：同时维护多个文件的一致性。

## 技术细节

先验证，再加锁，最后原子替换。

## 实践建议

- 始终先运行 dry-run。

## 局限性

- 测试数据不代表真实文章质量。

## Claude点评

确定性边界适合高风险文件写入。

## 关联知识

- [[现有笔记]]
"""


def append_body(date: str = "2026-08-02") -> str:
    return f"""## 补充 {date}

### 新增要点

- 新增内容。

### 新概念

- 新概念。

### 差异点

- 与原文不同。

### 点评补充

- 补充点评。
"""


def make_vault(root: Path) -> Path:
    vault = root / "vault"
    vault.mkdir()
    (vault / "_import-log.md").write_text(
        "# 导入日志\n\n"
        "| 时间 | 来源 URL | 写入路径 | 操作 |\n"
        "| --- | --- | --- | --- |\n"
        "| 2026-07-01 12:00:00 | https://old.example/a | [AI/Agent/old.md](AI/Agent/old.md) | 新建 |\n",
        encoding="utf-8",
    )
    (vault / "_knowledge-index.md").write_text(
        "# 知识索引\n\n"
        "_最后更新：2026-07-01_\n\n"
        "## AI / Agent\n\n"
        "| 文档 | type | 关键词 | 核心洞察 | 日期 |\n"
        "|------|------|--------|----------|------|\n"
        "| [旧文章](AI/Agent/old.md) | 观点 | `agent` | 旧洞察 | 2026-07-01 |\n",
        encoding="utf-8",
    )
    (vault / "AI/Agent").mkdir(parents=True)
    (vault / "AI/Agent/old.md").write_text("---\ntitle: 旧文章\n---\n", encoding="utf-8")
    return vault


def commit_process(script: str, plan: str, analysis: str, body: str, vault: str, timestamp: str, output: str):
    return subprocess.run(
        [
            os.environ.get("PYTHON", "python3"), script, "commit",
            "--plan-file", plan,
            "--analysis-file", analysis,
            "--body-file", body,
            "--vault", vault,
            "--timestamp", timestamp,
            "--output", output,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    ).returncode


class FixtureHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, headers, body = self.routes.get(self.path, (404, {}, b"not found"))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextlib.contextmanager
def fixture_server(routes):
    FixtureHandler.routes = routes
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class PlanTests(unittest.TestCase):
    def test_single_and_batch_preserve_order_and_deduplicate(self):
        single = ki.plan_from_raw("  --single https://example.com/a?x=1#f  ")
        self.assertEqual(single["mode"], "single")
        self.assertEqual(single["current_url"], "https://example.com/a?x=1#f")

        batch = ki.plan_from_raw(
            "说明 https://example.com/a https://example.com/b https://example.com/a"
        )
        self.assertEqual(batch["mode"], "batch")
        self.assertEqual(batch["input_urls"], ["https://example.com/a", "https://example.com/b"])

        forced = ki.plan_from_raw("--force https://example.com/a")
        self.assertEqual(forced["mode"], "single")
        self.assertEqual(forced["current_url"], "https://example.com/a")
        self.assertIs(forced["force"], True)

    def test_filter_rules_match_legacy_behavior(self):
        result = ki.plan_from_raw(
            " ".join([
                "https://mmbiz.qpic.cn/a.jpg",
                "https://mp.weixin.qq.com/s/",
                "https://example.com/a.pdf",
                "https://example.com/ok?x=1#part",
            ])
        )
        self.assertEqual(result["input_urls"], ["https://example.com/ok?x=1#part"])
        self.assertEqual([item["reason"] for item in result["rejected"]], [
            "微信 CDN 图片", "微信公众号文章路径不完整", "非网页内容"
        ])

    def test_no_url_and_invalid_single_have_legacy_messages(self):
        self.assertEqual(ki.plan_from_raw("hello")["message"], "❌ 未检测到有效 URL")
        self.assertEqual(
            ki.plan_from_raw("--single https://a.example https://b.example")["message"],
            "❌ 单条模式参数异常",
        )

    def test_empty_input_reports_argument_handoff_failure(self):
        result = ki.plan_from_raw("  \n")
        self.assertEqual(result["code"], "missing-input")
        self.assertEqual(
            result["message"],
            "❌ /ki 参数交接失败：未收到链接，请重新发送命令。",
        )
        self.assertEqual(result["rejected"], [])

    def test_shell_metacharacters_never_execute(self):
        result = ki.plan_from_raw("https://example.com/a$(touch%20SHOULD_NOT_EXIST)")
        self.assertEqual(result["mode"], "single")


class ScheduleTests(unittest.TestCase):
    def test_schedule_uses_argv_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            write_json(plan, {
                "status": "ok",
                "mode": "batch",
                "input_urls": ["https://a.example/x", "https://b.example/y"],
            })
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, "Timer created: abc\n", "")

            args = type("Args", (), {
                "plan_file": str(plan),
                "cc_connect": "/safe/cc-connect",
                "timeout": 30,
                "output": None,
            })()
            with mock.patch.object(ki.subprocess, "run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    ki.command_schedule(args)
            self.assertEqual(len(calls), 2)
            self.assertIsInstance(calls[0][0], list)
            self.assertNotIn("shell", calls[0][1])
            self.assertEqual(
                calls[0][0][-4:],
                ["--prompt", "/ki --single https://a.example/x", "--desc", "ki-batch"],
            )

    def test_partial_schedule_failure_deletes_created_timers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            write_json(plan, {
                "status": "ok",
                "mode": "batch",
                "input_urls": ["https://a.example/x", "https://b.example/y"],
            })
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if command[1:3] == ["timer", "del"]:
                    return subprocess.CompletedProcess(command, 0, "deleted\n", "")
                if "a.example" in command[-3]:
                    return subprocess.CompletedProcess(command, 0, "Timer created: timer-a\n", "")
                return subprocess.CompletedProcess(command, 1, "", "failed")

            args = type("Args", (), {
                "plan_file": str(plan),
                "cc_connect": "/safe/cc-connect",
                "timeout": 30,
                "output": str(root / "schedule.json"),
            })()
            with mock.patch.object(ki.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(ki.KiError):
                    with contextlib.redirect_stdout(io.StringIO()):
                        ki.command_schedule(args)
            payload = json.loads((root / "schedule.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["rolled_back"], 1)
            self.assertIn(["/safe/cc-connect", "timer", "del", "timer-a"], calls)


class FetchTests(unittest.TestCase):
    def test_private_urls_are_rejected_for_production_fetch(self):
        ok, reason = ki.validate_url("http://127.0.0.1:8080/private")
        self.assertFalse(ok)
        self.assertIn("公开网络", reason)
        with self.assertRaises(OSError):
            ki.assert_public_url("http://localhost/private")

    def test_curl_follows_308_redirect(self):
        body = (
            "<html><head><title>重定向后的页面</title></head><body>"
            + ("重定向正文" * 80)
            + "</body></html>"
        ).encode("utf-8")
        routes = {
            "/redirect": (308, {"Location": "/final"}, b""),
            "/final": (200, {"Content-Type": "text/html; charset=utf-8"}, body),
        }
        with fixture_server(routes) as base:
            source, charset = ki.curl_text(f"{base}/redirect", 3, allow_private=True)
        self.assertEqual(charset, "utf-8")
        self.assertIn("重定向后的页面", source)
        self.assertGreaterEqual(len(source), 200)

    def test_jina_success_skips_direct(self):
        text = "Title: Jina 标题\nPublished Time: 2026-08-01\nMarkdown Content:\n" + ("正文内容" * 80)
        with fixture_server({}) as base:
            target = f"{base}/article"
            FixtureHandler.routes = {
                "/jina/" + ki.urllib.parse.quote(target, safe=""): (
                    200,
                    {"Content-Type": "text/plain; charset=utf-8"},
                    text.encode(),
                ),
                "/article": (500, {}, b"direct endpoint must not be called"),
            }
            result = ki.fetch_article(target, 3, f"{base}/jina/{{url}}", allow_private=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "jina")
        self.assertEqual(result["title"], "Jina 标题")
        self.assertEqual(result["article_date"], "2026-08-01")
        self.assertEqual(result["direct_length"], 0)

    def test_jina_template_and_direct_fallback(self):
        generic = (
            '<html><head><title>普通网页标题</title>'
            '<meta property="article:published_time" content="2026-08-02T12:00:00Z">'
            '</head><body><article>' + ("普通正文" * 80) + '</article></body></html>'
        ).encode()
        routes = {
            "/jina": (200, {"Content-Type": "text/plain"}, b"too short"),
            "/article": (200, {"Content-Type": "text/html; charset=utf-8"}, generic),
        }
        with fixture_server(routes) as base:
            result = ki.fetch_article(f"{base}/article", 3, f"{base}/jina", allow_private=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "direct")
        self.assertEqual(result["title"], "普通网页标题")
        self.assertEqual(result["article_date"], "2026-08-02")
        self.assertGreaterEqual(result["content_length"], 200)

    def test_wechat_parser_extracts_title_date_and_body(self):
        html = (
            "<script>var msg_title = '微信测试标题'; var ct = '1785542400';</script>"
            '<div id="js_content">' + ("微信文章正文" * 60) + "</div>"
        )
        title, article_date, body = ki.parse_wechat(html)
        self.assertEqual(title, "微信测试标题")
        self.assertRegex(article_date, r"^\d{4}-\d{2}-\d{2}$")
        self.assertGreaterEqual(len(body), 200)

    def test_wechat_parser_handles_real_html_false_title_shape(self):
        html = (
            "<script>var msg_title = '为什么SFT用正向KL，RLHF用反向KL？'.html(false);"
            "var msg_desc = '后续字段'; var ct = '1785542400';</script>"
            '<div id="js_content">' + ("微信文章正文" * 60) + "</div>"
        )
        title, _, body = ki.parse_wechat(html)
        self.assertEqual(title, "为什么SFT用正向KL，RLHF用反向KL？")
        self.assertNotIn("msg_desc", title)
        self.assertGreaterEqual(len(body), 200)

    def test_direct_fallback_uses_curl_argv_without_shell(self):
        direct_html = (
            "<html><head><title>curl 标题</title></head><body>"
            + ("curl 正文" * 80)
            + "</body></html>"
        )
        def fake_run(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
                encoding="iso-8859-1",
            )
            Path(command[command.index("--output") + 1]).write_bytes(direct_html.encode("utf-8"))
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch.object(ki, "request_text", return_value=("short", "utf-8")):
            with mock.patch.object(ki.subprocess, "run", side_effect=fake_run) as run:
                result = ki.fetch_article(
                    "https://example.com/a$(touch%20SHOULD_NOT_EXIST)",
                    3,
                    "https://jina.invalid",
                    allow_private=True,
                )
        self.assertEqual(result["status"], "ok")
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], "https://example.com/a$(touch%20SHOULD_NOT_EXIST)")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_both_short_returns_exact_failure_shape(self):
        routes = {
            "/jina": (200, {"Content-Type": "text/plain"}, b"short"),
            "/article": (200, {"Content-Type": "text/html"}, b"<title>x</title><p>short</p>"),
        }
        with fixture_server(routes) as base:
            result = ki.fetch_article(f"{base}/article", 3, f"{base}/jina", allow_private=True)
        self.assertEqual(result["code"], "fetch-insufficient")
        self.assertIn("WebFetch:", result["message"])
        self.assertIn("curl:", result["message"])


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = make_vault(self.root)
        self.plan = self.root / "plan.json"
        self.analysis = self.root / "analysis.json"
        self.body = self.root / "body.md"
        write_json(self.plan, base_plan())
        write_json(self.analysis, new_analysis())
        self.body.write_text(full_body(), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_commit(self, *extra, env=None):
        command = [
            "python3", str(SCRIPT), "commit",
            "--plan-file", str(self.plan),
            "--analysis-file", str(self.analysis),
            "--body-file", str(self.body),
            "--vault", str(self.vault),
            "--timestamp", "2026-08-13 12:34:56",
            *extra,
        ]
        return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)

    def test_dry_run_makes_no_changes(self):
        before = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        result = self.run_commit("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_new_note_log_index_and_permissions(self):
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        note = self.vault / "AI/Agent/测试主题.md"
        text = note.read_text(encoding="utf-8")
        self.assertIn('sources:\n  - "https://example.com/article"', text)
        self.assertIn("date: 2026-08-01", text)
        for heading in ki.REQUIRED_BODY_SECTIONS:
            self.assertIn(heading, text)
        log = (self.vault / "_import-log.md").read_text(encoding="utf-8")
        self.assertIn("| 2026-08-13 12:34:56 | https://example.com/article |", log)
        self.assertLess(log.index("2026-08-13"), log.index("2026-07-01"))
        index = (self.vault / "_knowledge-index.md").read_text(encoding="utf-8")
        self.assertIn("_最后更新：2026-08-13_", index)
        self.assertIn("| [测试文章标题](AI/Agent/测试主题.md) | 实践案例 |", index)
        self.assertEqual(stat.S_IMODE(note.stat().st_mode), 0o644)

    def test_filename_collision_adds_suffix(self):
        (self.vault / "AI/Agent/测试主题.md").write_text("existing", encoding="utf-8")
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["path"], "AI/Agent/测试主题-2.md")
        self.assertEqual((self.vault / "AI/Agent/测试主题.md").read_text(), "existing")

    def test_duplicate_stops_without_writes(self):
        original = (self.vault / "_import-log.md").read_text()
        with (self.vault / "_import-log.md").open("a", encoding="utf-8") as handle:
            handle.write("| 2026-08-01 00:00:00 | https://example.com/article | [AI/Agent/existing.md](AI/Agent/existing.md) | 新建 |\n")
        expected = (self.vault / "_import-log.md").read_text()
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "duplicate")
        self.assertEqual((self.vault / "_import-log.md").read_text(), expected)
        self.assertFalse((self.vault / "AI/Agent/测试主题.md").exists())
        self.assertNotEqual(original, expected)

    def test_force_plan_explicitly_bypasses_duplicate_guard(self):
        existing = self.vault / "AI/Agent/existing.md"
        existing.write_text("old content", encoding="utf-8")
        with (self.vault / "_import-log.md").open("a", encoding="utf-8") as handle:
            handle.write(
                "| 2026-08-01 00:00:00 | https://example.com/article | "
                "[AI/Agent/existing.md](AI/Agent/existing.md) | 新建 |\n"
            )
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        plan["force"] = True
        write_json(self.plan, plan)
        analysis = new_analysis(path="AI/Agent/existing.md")
        write_json(self.analysis, analysis)
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        self.assertIn("## 摘要", existing.read_text(encoding="utf-8"))
        self.assertFalse((self.vault / "AI/Agent/测试主题.md").exists())
        self.assertEqual(
            (self.vault / "_import-log.md").read_text().count("https://example.com/article"),
            2,
        )

    def test_force_cannot_overwrite_a_different_path(self):
        existing = self.vault / "AI/Agent/existing.md"
        existing.write_text("do not change", encoding="utf-8")
        with (self.vault / "_import-log.md").open("a", encoding="utf-8") as handle:
            handle.write(
                "| 2026-08-01 00:00:00 | https://example.com/article | "
                "[AI/Agent/existing.md](AI/Agent/existing.md) | 新建 |\n"
            )
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        plan["force"] = True
        write_json(self.plan, plan)
        result = self.run_commit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("原路径完全一致", result.stderr)
        self.assertEqual(existing.read_text(encoding="utf-8"), "do not change")

    def test_rejects_mismatched_current_url_and_path_traversal(self):
        bad = new_analysis()
        bad["current_url"] = "https://evil.example"
        write_json(self.analysis, bad)
        result = self.run_commit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("内部保护", result.stderr)

        bad = new_analysis(path="../escape.md")
        write_json(self.analysis, bad)
        result = self.run_commit()
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "escape.md").exists())

        bad = new_analysis(path="AI/Agent/路径.md")
        bad.update({"top_level": "工具", "second_level": "自动化"})
        write_json(self.analysis, bad)
        result = self.run_commit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("不一致", result.stderr)

        bad = new_analysis(path="AI/Agent/name|pipe.md")
        write_json(self.analysis, bad)
        result = self.run_commit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("表格非法字符", result.stderr)

    def test_yaml_special_values_remain_strings(self):
        analysis = new_analysis()
        analysis["tags"] = ["#topic", "yes", "a: b", "[nested]", "null"]
        analysis["type"] = "a: b"
        write_json(self.analysis, analysis)
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.vault / "AI/Agent/测试主题.md").read_text(encoding="utf-8")
        for value in analysis["tags"]:
            self.assertIn(f"  - {json.dumps(value, ensure_ascii=False)}", text)
        self.assertIn('type: "a: b"', text)

    def test_pipe_url_can_be_looked_up_after_commit(self):
        url = "https://example.com/a|b"
        write_json(self.plan, base_plan(url))
        write_json(self.analysis, new_analysis(url=url))
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ki.find_log_url(self.vault / "_import-log.md", url), "AI/Agent/测试主题.md")

    def test_legacy_blank_log_rows_are_repaired(self):
        log_path = self.vault / "_import-log.md"
        log_path.write_text(log_path.read_text().replace("| --- | --- | --- | --- |\n", "\n| --- | --- | --- | --- |\n\n"))
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = log_path.read_text(encoding="utf-8")
        self.assertNotIn("\n\n| --- |", text)
        self.assertNotIn("| --- | --- | --- | --- |\n\n", text)

    def test_failure_removes_new_empty_directories_and_journal(self):
        analysis = new_analysis(path="全新顶层/全新二级/测试主题.md")
        analysis.update({"top_level": "全新顶层", "second_level": "全新二级"})
        write_json(self.analysis, analysis)
        env = os.environ.copy()
        env["KI_FAIL_AFTER_REPLACE"] = "note"
        result = self.run_commit(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.vault / "全新顶层").exists())
        self.assertFalse((self.vault / ".ki-import-transaction.json").exists())

    def test_failure_preserves_preexisting_empty_directories(self):
        existing = self.vault / "UserEmpty/KeepMe"
        existing.mkdir(parents=True)
        analysis = new_analysis(path="UserEmpty/KeepMe/测试主题.md")
        analysis.update({"top_level": "UserEmpty", "second_level": "KeepMe"})
        write_json(self.analysis, analysis)
        env = os.environ.copy()
        env["KI_FAIL_AFTER_REPLACE"] = "note"
        result = self.run_commit(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(existing.is_dir())

    def test_stale_journal_is_recovered_before_next_commit(self):
        note = self.vault / "AI/Agent/old.md"
        original = note.read_bytes()
        journal = ki.write_journal(self.vault, {note: ki.snapshot(note)})
        note.write_text("partial transaction", encoding="utf-8")
        self.assertTrue(journal.exists())
        write_json(self.plan, base_plan("https://example.com/new-after-recovery"))
        write_json(self.analysis, new_analysis(url="https://example.com/new-after-recovery"))
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(note.read_bytes(), original)
        self.assertFalse(journal.exists())
        self.assertIs(json.loads(result.stdout)["recovered_previous_transaction"], True)

    def test_lookup_recovers_stale_journal_before_duplicate_check(self):
        note = self.vault / "AI/Agent/old.md"
        original = note.read_bytes()
        journal = ki.write_journal(self.vault, {note: ki.snapshot(note)})
        note.write_text("partial transaction", encoding="utf-8")
        args = type("Args", (), {
            "plan_file": str(self.plan),
            "vault": str(self.vault),
            "output": None,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ki.command_lookup(args)
        payload = json.loads(output.getvalue())
        self.assertIs(payload["recovered_previous_transaction"], True)
        self.assertEqual(note.read_bytes(), original)
        self.assertFalse(journal.exists())

    def test_append_preserves_unknown_fields_and_original_date(self):
        target = self.vault / "AI/Agent/existing.md"
        target.write_text(
            "---\n"
            'title: "已有笔记"\n'
            "sources:\n  - \"https://old.example/source\"\n"
            "date: 2026-01-02\n"
            "created: 2026-01-03\n"
            "updated: 2026-01-03\n"
            "tags: [agent, workflow]\n"
            "domain: AI/Agent/工程实践\n"
            "custom: keep-me\n"
            "type: 观点\n"
            "---\n\n## 摘要\n\n原正文。\n",
            encoding="utf-8",
        )
        analysis = new_analysis(path="AI/Agent/existing.md")
        analysis.update({"action": "append", "tags": ["agent", "memory", "实践案例"]})
        write_json(self.analysis, analysis)
        self.body.write_text(append_body(), encoding="utf-8")
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = target.read_text(encoding="utf-8")
        self.assertIn("custom: keep-me", text)
        self.assertIn("domain: AI/Agent/工程实践", text)
        self.assertIn('  - "https://example.com/article"', text)
        self.assertIn('  - "memory"', text)
        self.assertIn("## 补充 2026-08-02", text)
        index = (self.vault / "_knowledge-index.md").read_text(encoding="utf-8")
        self.assertIn("| [已有笔记](AI/Agent/existing.md) | 观点 |", index)
        self.assertIn("| 2026-01-02 |", index)

    def test_failure_after_each_replace_rolls_back_all_files(self):
        before = {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        for point in ("note", "log", "index"):
            env = os.environ.copy()
            env["KI_FAIL_AFTER_REPLACE"] = point
            result = self.run_commit(env=env)
            self.assertEqual(result.returncode, 1, (point, result.stderr))
            after = {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
            self.assertEqual(before, after, point)

    def test_concurrent_same_url_commits_once(self):
        outputs = [self.root / f"out-{i}.json" for i in range(8)]
        args = [
            (str(SCRIPT), str(self.plan), str(self.analysis), str(self.body), str(self.vault),
             f"2026-08-13 12:35:{i:02d}", str(outputs[i]))
            for i in range(8)
        ]
        with multiprocessing.Pool(8) as pool:
            codes = pool.starmap(commit_process, args)
        self.assertEqual(codes, [0] * 8)
        statuses = [json.loads(path.read_text())["status"] for path in outputs]
        self.assertEqual(statuses.count("ok"), 1)
        self.assertEqual(statuses.count("duplicate"), 7)
        log = (self.vault / "_import-log.md").read_text(encoding="utf-8")
        self.assertEqual(log.count("https://example.com/article"), 1)
        self.assertEqual(len(list((self.vault / "AI/Agent").glob("测试主题*.md"))), 1)


class ScanTests(unittest.TestCase):
    def test_scan_limits_to_domain_and_first_60_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            (vault / "AI").mkdir(parents=True)
            (vault / "工具").mkdir()
            (vault / "AI/a.md").write_text("agent workflow\n" + "x\n" * 70 + "late-term", encoding="utf-8")
            (vault / "工具/b.md").write_text("agent workflow memory", encoding="utf-8")
            terms = root / "terms.txt"
            terms.write_text("agent\nworkflow\nmemory\nlate-term\n", encoding="utf-8")
            out = root / "scan.json"
            result = subprocess.run([
                "python3", str(SCRIPT), "scan", "--vault", str(vault), "--domain", "ai",
                "--terms-file", str(terms), "--output", str(out),
            ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads(out.read_text())["candidates"]
            self.assertEqual([item["path"] for item in candidates], ["AI/a.md"])
            self.assertEqual(candidates[0]["term_hits"], 2)


if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    unittest.main(verbosity=2)
