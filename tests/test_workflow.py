"""Development regressions using temporary vaults; no model, network or real timers."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ki_import as ki


URL = "https://mp.weixin.qq.com/s/quiet-test-one"
URL_TWO = "https://mp.weixin.qq.com/s/quiet-test-two"
RELATIVE = "AI/LLM/长文本处理/有效上下文.md"
BODY = "\n\n".join(heading + "\n\n保留原文中的证据与结论。" for heading in ki.REQUIRED_BODY_SECTIONS)


class QuietWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ki-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.temp_patch = patch.object(tempfile, "tempdir", str(self.root))
        self.temp_patch.start()
        self.addCleanup(self.temp_patch.stop)
        self.network = patch.object(ki, "fetch_article", side_effect=self.article)
        self.fetch = self.network.start()
        self.addCleanup(self.network.stop)

    @staticmethod
    def article(url, timeout, jina_base):
        return {"status": "ok", "source_url": url, "title": "有效上下文",
                "article_date": "2026-09-19", "article_text": "原始文章证据。" * 80,
                "method": "test", "content_length": 640, "attempts": []}

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ki.main(list(args))
        self.assertEqual(err.getvalue(), "", err.getvalue())
        return code, json.loads(out.getvalue())

    def new_run(self, raw=URL):
        code, result = self.run_cli("init")
        self.assertEqual(code, 0)
        artifacts = result["artifacts"]
        Path(artifacts["raw_file"]).write_text(raw, encoding="utf-8")
        return artifacts

    def prepare(self, artifacts, *extra):
        return self.run_cli("prepare", "--input-file", artifacts["raw_file"],
                            "--vault", str(self.vault), *extra)

    def draft(self, artifacts, url=URL, action="new", body=BODY):
        meta = {"current_url": url, "action": action, "relative_path": RELATIVE,
                "title": "有效上下文", "article_date": "2026-09-19", "type": "观点",
                "tags": ["llm", "attention", "benchmark"], "top_level": "AI",
                "second_level": "LLM", "insight": "有效长度应以实际任务评测为准"}
        Path(artifacts["analysis_file"]).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        Path(artifacts["body_file"]).write_text(body, encoding="utf-8")

    def finish(self, artifacts):
        return self.run_cli("finish", "--plan-file", artifacts["plan_file"], "--vault", str(self.vault))

    def seed(self):
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        self.assertEqual(self.finish(artifacts)[0], 0)
        return artifacts

    def test_verified_fetch_author_is_committed_to_shared_log(self):
        store = ki.unified_store_module()
        (self.vault / store.INDEX_NAME).write_bytes(store.render_index([]))
        (self.vault / store.LOG_NAME).write_bytes(store.render_log([]))
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        analysis_file = Path(artifacts["analysis_file"])
        meta = json.loads(analysis_file.read_text())
        meta.update(relative_path="知识库/AI/大模型/推理与成本/长上下文/有效上下文.md", second_level="大模型")
        analysis_file.write_text(json.dumps(meta))
        article_file = Path(artifacts["article_file"])
        article = json.loads(article_file.read_text())
        article["author_name"] = "公众号甲"
        article_file.write_text(json.dumps(article))
        with patch.object(ki, "configured_unified_only", return_value=True), \
             patch.object(ki, "configured_notes_root", return_value="知识库"):
            code, result = self.finish(artifacts)
        self.assertEqual((code, result["status"]), (0, "ok"))
        _, logs = store.load(self.vault)
        self.assertEqual(logs[0][-1], "公众号甲")
        self.assertTrue((self.vault / result["path"]).is_file())

    def test_unified_topic_directory_accepts_new_note_and_deduplicates_url(self):
        relative = "知识库/AI/大模型/推理与成本/长上下文/有效上下文.md"
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        analysis_file = Path(artifacts["analysis_file"])
        meta = json.loads(analysis_file.read_text(encoding="utf-8"))
        meta.update(relative_path=relative, second_level="大模型")
        analysis_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with patch.object(ki, "configured_notes_root", return_value="知识库"):
            code, result = self.finish(artifacts)
        self.assertEqual((code, result["status"], result["path"]), (0, "ok", relative))
        self.assertTrue((self.vault / relative).is_file())
        self.assertIn(f"source_url: {ki.yaml_quote(URL)}", (self.vault / relative).read_text(encoding="utf-8"))
        self.assertIn("## AI / 大模型 / 推理与成本 / 长上下文",
                      (self.vault / "_knowledge-index.md").read_text(encoding="utf-8"))
        duplicate = self.new_run()
        code, repeated = self.prepare(duplicate)
        self.assertEqual((code, repeated["status"]), (0, "duplicate"))
        self.assertEqual(repeated["path"], relative)

    def test_unified_only_writes_the_two_visible_catalogs(self):
        import sys
        scripts = Path(__file__).resolve().parents[2] / "douyin-to-obsidian" / "scripts"
        sys.path.insert(0, str(scripts))
        import unified_store
        (self.vault / unified_store.INDEX_NAME).write_bytes(unified_store.render_index([]))
        (self.vault / unified_store.LOG_NAME).write_bytes(unified_store.render_log([]))
        relative = "知识库/AI/大模型/推理与成本/长上下文/有效上下文.md"
        artifacts = self.new_run()
        with patch.object(ki, "configured_unified_only", return_value=True), patch.object(
                ki, "configured_notes_root", return_value="知识库"):
            self.assertEqual(self.prepare(artifacts)[0], 0)
            self.draft(artifacts)
            analysis_file = Path(artifacts["analysis_file"])
            meta = json.loads(analysis_file.read_text(encoding="utf-8"))
            meta.update(relative_path=relative, second_level="大模型")
            analysis_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            code, result = self.finish(artifacts)
            self.assertEqual((code, result["status"], result["path"]), (0, "ok", relative))
            index, log = unified_store.load(self.vault)
            self.assertEqual((len(index), len(log)), (1, 1))
            self.assertEqual(unified_store.find_ki_url(self.vault, URL), relative)
            duplicate = self.new_run()
            code, repeated = self.prepare(duplicate)
            self.assertEqual((code, repeated["status"], repeated["path"]), (0, "duplicate", relative))
            append = self.new_run(URL_TWO)
            self.assertEqual(self.prepare(append)[0], 0)
            self.draft(append, url=URL_TWO, action="append",
                       body="## 补充 2026-09-20\n\n### 新增要点\n第二来源的新证据\n")
            append_meta_path = Path(append["analysis_file"])
            append_meta = json.loads(append_meta_path.read_text(encoding="utf-8"))
            append_meta.update(relative_path=relative, second_level="大模型")
            append_meta_path.write_text(json.dumps(append_meta, ensure_ascii=False), encoding="utf-8")
            code, result = self.finish(append)
            self.assertEqual((code, result["operation"]), (0, "append"))
            index, log = unified_store.load(self.vault)
            self.assertEqual((len(index), len(log)), (1, 2))
            self.assertEqual(unified_store.find_ki_url(self.vault, URL_TWO), relative)
        self.assertFalse((self.vault / "_knowledge-index.md").exists())
        self.assertFalse((self.vault / "_import-log.md").exists())

    def test_unified_only_failed_log_write_restores_totals_and_note(self):
        scripts = Path(__file__).resolve().parents[2] / "douyin-to-obsidian" / "scripts"
        import sys
        sys.path.insert(0, str(scripts))
        import unified_store
        totals = {self.vault / unified_store.INDEX_NAME: unified_store.render_index([]),
                  self.vault / unified_store.LOG_NAME: unified_store.render_log([])}
        for path, content in totals.items():
            path.write_bytes(content)
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        meta_path = Path(artifacts["analysis_file"])
        meta = json.loads(meta_path.read_text())
        meta.update(relative_path="知识库/AI/大模型/推理与成本/长上下文/有效上下文.md",
                    second_level="大模型")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
        with patch.object(ki, "configured_unified_only", return_value=True), patch.object(
                ki, "configured_notes_root", return_value="知识库"), patch.dict(
                os.environ, {"KI_FAIL_AFTER_REPLACE": "log"}):
            code, result = self.finish(artifacts)
        self.assertEqual((code, result["stage"]), (1, "写入"))
        self.assertEqual({path: path.read_bytes() for path in totals}, totals)
        self.assertFalse((self.vault / meta["relative_path"]).exists())

    def test_unified_view_failure_keeps_successful_import(self):
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        with patch.object(ki, "sync_unified_catalog", return_value="synthetic view failure"):
            code, result = self.finish(artifacts)
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertEqual(result["unified_sync_warning"], "synthetic view failure")
        self.assertIn("总索引/总日志同步失败", result["message"])
        self.assertTrue((self.vault / RELATIVE).is_file())

    def test_scan_ignores_both_source_and_unified_catalogs(self):
        marker = "only-catalog-marker"
        for name in ("知识总索引.md", "导入总日志.md", "_knowledge-index.md", "_import-log.md",
                     "DY-知识索引.md", "DY-更新日志.md"):
            (self.vault / name).write_text(marker)
        terms = self.root / "terms.txt"
        terms.write_text(marker)
        code, result = self.run_cli("scan", "--vault", str(self.vault), "--domain", "all", "--terms-file", str(terms))
        self.assertEqual((code, result["candidate_count"]), (0, 0))

    def test_prepare_returns_full_article_once_and_saves_steps(self):
        artifacts = self.new_run()
        code, result = self.prepare(artifacts)
        self.assertEqual((code, result["status"]), (0, "ready"))
        self.assertEqual(result["article_text"], "原始文章证据。" * 80)
        self.assertNotIn("artifacts", result)
        self.assertNotIn("attempts", result)
        self.fetch.assert_called_once()
        for name in ("plan_file", "lookup_file", "article_file", "prepare_file"):
            self.assertTrue(Path(artifacts[name]).is_file())

    def test_duplicate_stops_before_fetch(self):
        self.seed()
        self.fetch.reset_mock()
        artifacts = self.new_run()
        code, result = self.prepare(artifacts)
        self.assertEqual((code, result["status"], result["path"]), (0, "duplicate", RELATIVE))
        self.assertNotIn("\n", result["message"])
        self.fetch.assert_not_called()
        self.assertFalse(Path(artifacts["article_file"]).exists())
        self.assertTrue(Path(artifacts["plan_file"]).is_file())

    def test_batch_preserves_order_deduplication_and_fresh_sessions(self):
        artifacts = self.new_run(URL + " " + URL_TWO + " " + URL)
        results = [subprocess.CompletedProcess([], 0, "Timer created: one\n", ""),
                   subprocess.CompletedProcess([], 0, "Timer created: two\n", "")]
        with patch.object(ki.subprocess, "run", side_effect=results) as scheduled:
            code, result = self.prepare(artifacts, "--cc-connect", "/test/cc-connect")
        self.assertEqual((code, result["status"], result["scheduled"]), (0, "queued", 2))
        commands = [call.args[0] for call in scheduled.call_args_list]
        self.assertEqual([command[command.index("--prompt") + 1] for command in commands],
                         ["/ki --single " + URL, "/ki --single " + URL_TWO])
        self.assertTrue(all("new-per-run" in command for command in commands))
        self.assertNotIn("https://", result["message"])
        self.fetch.assert_not_called()

    def test_failed_batch_reports_stage_and_rolls_back_created_timer(self):
        artifacts = self.new_run(URL + " " + URL_TWO)
        results = [subprocess.CompletedProcess([], 0, "Timer created: one\n", ""),
                   subprocess.CompletedProcess([], 1, "", "rejected"),
                   subprocess.CompletedProcess([], 0, "deleted", "")]
        with patch.object(ki.subprocess, "run", side_effect=results) as scheduled:
            code, result = self.prepare(artifacts, "--cc-connect", "/test/cc-connect")
        self.assertEqual((code, result["status"], result["stage"]), (2, "error", "排队"))
        self.assertEqual(scheduled.call_args_list[-1].args[0], ["/test/cc-connect", "timer", "del", "one"])
        self.assertEqual(ki.read_json(artifacts["schedule_file"])["rolled_back"], 1)
        self.fetch.assert_not_called()

    def test_fetch_failure_reports_stage_without_import_or_paywall_guess(self):
        artifacts = self.new_run()
        self.fetch.side_effect = None
        self.fetch.return_value = {"status": "error", "message": "直连超时；\nJina 超时", "attempts": []}
        code, result = self.prepare(artifacts)
        self.assertEqual((code, result["stage"]), (2, "抓取"))
        self.assertIn("超时", result["message"])
        self.assertNotIn("\n", result["message"])
        self.assertIn("\n", ki.read_json(artifacts["prepare_file"])["detail"])
        self.assertNotIn("付费", result["message"])
        self.assertFalse((self.vault / "_import-log.md").exists())
        self.assertEqual(ki.read_json(artifacts["article_file"])["status"], "error")

    def test_invalid_input_stops_before_fetch(self):
        artifacts = self.new_run("")
        code, result = self.prepare(artifacts)
        self.assertEqual((code, result["stage"]), (2, "参数校验"))
        self.fetch.assert_not_called()

    def test_finish_creates_note_log_index_and_removes_temporary_run(self):
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        code, result = self.finish(artifacts)
        self.assertEqual(code, 0)
        self.assertEqual(result["operation"], "new")
        self.assertEqual(result["message"], "✅ 新增成功：" + RELATIVE)
        self.assertNotIn("table", result)
        self.assertFalse(Path(artifacts["raw_file"]).parent.exists())
        for relative in (RELATIVE, "_import-log.md", "_knowledge-index.md"):
            self.assertTrue((self.vault / relative).is_file())
        self.assertIn(URL, (self.vault / RELATIVE).read_text(encoding="utf-8"))

    def test_unexpected_run_file_preserves_diagnostics_after_commit(self):
        artifacts = self.new_run()
        self.assertEqual(self.prepare(artifacts)[0], 0)
        self.draft(artifacts)
        unexpected = Path(artifacts["raw_file"]).parent / "keep.txt"
        unexpected.write_text("not part of /ki", encoding="utf-8")
        code, result = self.finish(artifacts)
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertIn("清理失败", result["message"])
        self.assertTrue(unexpected.is_file())
        self.assertTrue(Path(artifacts["finish_file"]).is_file())
        self.assertTrue((self.vault / RELATIVE).is_file())
        self.assertIn(URL, (self.vault / "_import-log.md").read_text(encoding="utf-8"))

    def test_force_updates_original_path_and_reports_update(self):
        self.seed()
        artifacts = self.new_run("--force " + URL)
        code, prepared = self.prepare(artifacts)
        self.assertEqual((code, prepared["existing_path"]), (0, RELATIVE))
        self.draft(artifacts, body=BODY + "\n更新证据\n")
        code, result = self.finish(artifacts)
        self.assertEqual((code, result["operation"]), (0, "update"))
        self.assertEqual(result["message"], "✅ 更新成功：" + RELATIVE)
        self.assertIn("更新证据", (self.vault / RELATIVE).read_text(encoding="utf-8"))
        self.assertIn("| 更新 |", (self.vault / "_import-log.md").read_text(encoding="utf-8"))
        self.assertEqual(len(list((self.vault / "AI").rglob("*.md"))), 1)

    def test_append_preserves_original_and_adds_source(self):
        self.seed()
        artifacts = self.new_run(URL_TWO)
        self.prepare(artifacts)
        self.draft(artifacts, url=URL_TWO, action="append", body="## 补充 2026-09-20\n\n### 新增要点\n新的证据\n")
        code, result = self.finish(artifacts)
        self.assertEqual((code, result["operation"]), (0, "append"))
        note = (self.vault / RELATIVE).read_text(encoding="utf-8")
        self.assertIn(f"source_url: {ki.yaml_quote(URL)}", note)
        self.assertNotIn(f"source_url: {ki.yaml_quote(URL_TWO)}", note)
        for text in (URL, URL_TWO, "date: 2026-09-19", "## 摘要", "新的证据"):
            self.assertIn(text, note)
        self.assertIn("追加内容", result["message"])

    def test_append_legacy_source_keeps_original_link(self):
        legacy = "\n".join(["---", "title: Legacy", f"source: {ki.yaml_quote(URL)}",
                            "date: 2026-09-19", "created: 2026-09-19", "tags:",
                            "  - llm", "type: 观点", "---", "", "## 摘要", "旧内容", ""])
        updated, _ = ki.update_append_note(legacy, URL_TWO, "2026-09-20", ["agent"],
                                            "## 补充 2026-09-20\n\n### 新增要点\n新内容")
        self.assertIn(f"source_url: {ki.yaml_quote(URL)}", updated)
        self.assertIn(f"  - {ki.yaml_quote(URL)}", updated)
        self.assertIn(f"  - {ki.yaml_quote(URL_TWO)}", updated)

    def test_invalid_draft_never_reaches_actual_commit(self):
        artifacts = self.new_run()
        self.prepare(artifacts)
        self.draft(artifacts, url=URL_TWO)
        code, result = self.finish(artifacts)
        self.assertEqual((code, result["stage"]), (2, "提交校验"))
        self.assertFalse(Path(artifacts["commit_file"]).exists())
        self.assertFalse((self.vault / RELATIVE).exists())
        self.assertFalse((self.vault / "_import-log.md").exists())
        self.assertTrue(Path(artifacts["raw_file"]).parent.is_dir())

    def test_each_write_failure_rolls_back_all_three_files(self):
        self.seed()
        paths = [self.vault / relative for relative in (RELATIVE, "_import-log.md", "_knowledge-index.md")]
        before = {path: path.read_bytes() for path in paths}
        for failure in ("note", "log", "index"):
            with self.subTest(failure=failure):
                artifacts = self.new_run("--force " + URL)
                self.prepare(artifacts)
                self.draft(artifacts, body=BODY + "\n不能残留的改动\n")
                with patch.dict(os.environ, {"KI_FAIL_AFTER_REPLACE": failure}):
                    code, result = self.finish(artifacts)
                self.assertEqual((code, result["stage"]), (1, "写入"))
                self.assertNotIn("✅", result["message"])
                self.assertEqual(before, {path: path.read_bytes() for path in paths})
                self.assertTrue(Path(artifacts["raw_file"]).parent.is_dir())

    def test_foreign_run_artifact_is_rejected(self):
        artifacts = self.new_run()
        other = self.new_run(URL_TWO)
        self.prepare(artifacts)
        plan = ki.read_json(artifacts["plan_file"])
        plan["artifacts"]["body_file"] = other["body_file"]
        Path(artifacts["plan_file"]).write_text(json.dumps(plan), encoding="utf-8")
        code, result = self.finish(artifacts)
        self.assertEqual((code, result["stage"]), (2, "提交校验"))
        self.assertFalse((self.vault / RELATIVE).exists())

    def test_compact_scan_keeps_full_saved_candidates(self):
        folder = self.vault / "AI" / "LLM"
        folder.mkdir(parents=True)
        for index in range(8):
            (folder / f"context-{index}.md").write_text("context\n" + "已有笔记信息。" * 150, encoding="utf-8")
        artifacts = self.new_run()
        self.prepare(artifacts)
        Path(artifacts["terms_file"]).write_text("context\n", encoding="utf-8")
        code, result = self.run_cli("scan", "--plan-file", artifacts["plan_file"],
            "--vault", str(self.vault), "--domain", "ai", "--terms-file", artifacts["terms_file"],
            "--output", artifacts["scan_file"], "--compact")
        saved = ki.read_json(artifacts["scan_file"])
        self.assertEqual(code, 0)
        self.assertEqual((len(result["candidates"]), len(saved["candidates"])), (6, 8))
        self.assertTrue(result["more_stored_candidates"])
        self.assertTrue(all(len(item["excerpt"]) <= 350 for item in result["candidates"]))
        self.assertTrue(all(len(item["excerpt"]) == 800 for item in saved["candidates"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
