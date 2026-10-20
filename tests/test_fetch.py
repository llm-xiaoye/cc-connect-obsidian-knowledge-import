"""Focused fetch regressions; local inputs only, no model or Vault writes."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ki_import as ki


URL = "https://mp.weixin.qq.com/s/fixture"
BODY = "正文段落包含应完整保留的知识和结尾。" * 20
HTML = '<div id="js_content"><section><p><span>' + BODY + '</span></p></section></div>'
JINA = "Title: fixture\nMarkdown Content:\n" + BODY


class TextExtractionTests(unittest.TestCase):
    def test_nested_article_keeps_later_paragraphs_and_excludes_outside_text(self):
        first = "首" * 140
        source = ('<div>导航</div><div id="js_content"><section><p><span>'
                  + first + '</span></p><p>中间<strong>重点</strong></p>'
                  '<section><p>最后一段</p></section></section></div><p>页脚</p>')
        self.assertEqual(ki.strip_html(source, "#js_content"), first + "\n\n中间重点\n\n最后一段")

    def test_excluded_elements_void_tags_and_entities(self):
        source = ('<div id="js_content"><p>甲&amp;乙<br>丙<img src="x"><span>丁</span></p>'
                  '<script>不要执行()</script><style>.secret{}</style>'
                  '<svg><text>图形脚本</text></svg><noscript>占位符</noscript>'
                  '<p>最终内容</p></div>')
        body = ki.strip_html(source, "#js_content")
        self.assertEqual(body, "甲&乙\n丙丁\n\n最终内容")

    def test_missing_article_container_is_not_whole_page_text(self):
        self.assertEqual(ki.strip_html("<p>环境异常，请完成验证</p>", "#js_content"), "")

    def test_selects_first_container_and_handles_self_closing_tags(self):
        self.assertEqual(ki.strip_html('<div id="js_content">甲<span/>乙<br/>丙</div>'
                                      '<div id="js_content">重复区域</div>', "#js_content"), "甲乙\n丙")

    def test_generic_html_retains_full_text_after_nested_tags(self):
        self.assertEqual(ki.strip_html("<main><p>首<b>段</b></p><p>末段</p></main>"), "首段\n\n末段")


class FetchRoutingTests(unittest.TestCase):
    def test_wechat_direct_success_skips_proxy(self):
        with patch.object(ki, "curl_text", return_value=(HTML, "utf-8")) as direct, \
             patch.object(ki, "request_text", side_effect=AssertionError("proxy must not run")):
            result = ki.fetch_article(URL, 5, "https://r.jina.ai")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "direct")
        self.assertEqual(result["article_text"], BODY)
        self.assertEqual([item["method"] for item in result["attempts"]], ["direct"])
        direct.assert_called_once()

    def test_wechat_direct_failure_uses_proxy(self):
        with patch.object(ki, "curl_text", side_effect=OSError("HTTP 状态 403")), \
             patch.object(ki, "request_text", return_value=(JINA, "utf-8")):
            result = ki.fetch_article(URL, 5, "https://r.jina.ai")
        self.assertEqual(result["method"], "jina")
        self.assertEqual([item["method"] for item in result["attempts"]], ["direct", "jina"])

    def test_generic_page_preserves_proxy_then_direct_fallback(self):
        with patch.object(ki, "request_text", side_effect=OSError("connection timeout")), \
             patch.object(ki, "curl_text", return_value=(HTML, "utf-8")):
            result = ki.fetch_article("https://example.org/article", 5, "https://r.jina.ai")
        self.assertEqual(result["method"], "direct")
        self.assertEqual([item["method"] for item in result["attempts"]], ["jina", "direct"])

    def test_short_free_article_is_not_reported_as_paywall(self):
        with patch.object(ki, "curl_text", return_value=('<div id="js_content">' + "文" * 140 + "</div>", "utf-8")), \
             patch.object(ki, "request_text", side_effect=OSError("curl 退出码 28: Timeout was reached")):
            result = ki.fetch_article(URL, 5, "https://r.jina.ai")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["direct_length"], 140)
        self.assertNotIn("可能付费墙", result["message"])
        self.assertNotIn("WebFetch", result["message"])
        self.assertIn("Jina 代理连接或响应超时", result["message"])
        self.assertIn("不能据此判断文章收费", result["message"])

    def test_http_error_body_is_not_accepted_as_article(self):
        def denied(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text("HTTP/2 403 Forbidden\r\n\r\n")
            Path(command[command.index("--output") + 1]).write_text(HTML)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with patch.object(ki, "public_addresses", return_value=["93.184.216.34"]), \
             patch.object(ki.subprocess, "run", side_effect=denied):
            with self.assertRaisesRegex(OSError, "HTTP 状态 403"):
                ki.curl_text(URL, 5)


class AuthorMetadataTests(unittest.TestCase):
    def test_wechat_byline_precedes_account_and_body_mentions_are_not_authors(self):
        source = '<span id="js_author_name">张三</span><a id="js_name">公众号甲</a>' + HTML
        self.assertEqual(ki.source_author_name(source, wechat=True), "张三")
        self.assertEqual(ki.source_author_name('<a id="js_name">公众号甲</a>' + HTML, wechat=True), "公众号甲")
        self.assertEqual(ki.source_author_name(HTML + '<p>作者可能是张三</p>', wechat=True), "")

    def test_explicit_js_account_and_generic_meta_are_decoded(self):
        self.assertEqual(ki.source_author_name("var nick_name = '公众号&amp;甲';", wechat=True), "公众号&甲")
        self.assertEqual(ki.source_author_name('<meta content="甲&amp;乙" name="author">'), "甲&乙")
        self.assertEqual(ki.source_author_name('<meta name="author" content="https://example.com/id">'), "")

    def test_fetch_saves_verified_author_and_keeps_unknown_empty(self):
        with patch.object(ki, "curl_text", return_value=('<a id="js_name">公众号甲</a>' + HTML, "utf-8")):
            self.assertEqual(ki.fetch_article(URL, 5, "https://r.jina.ai")["author_name"], "公众号甲")
        with patch.object(ki, "curl_text", side_effect=OSError("blocked")), \
             patch.object(ki, "request_text", return_value=(JINA, "utf-8")):
            self.assertEqual(ki.fetch_article(URL, 5, "https://r.jina.ai")["author_name"], "")

    def test_log_author_must_come_from_this_runs_fetched_article(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root).resolve()
            path = directory / "article.json"
            plan = {"current_url": URL, "run_id": "current", "run_dir": str(directory),
                    "artifacts": {"article_file": str(path)}}
            article = {"status": "ok", "source_url": URL, "author_name": "公众号甲",
                       "run_id": plan["run_id"], "run_dir": plan["run_dir"], "artifacts": plan["artifacts"]}
            path.write_text(json.dumps(article))
            self.assertEqual(ki.article_author_name(plan), "公众号甲")
            article["run_id"] = "other"
            path.write_text(json.dumps(article))
            with self.assertRaises(ki.KiError):
                ki.article_author_name(plan)
            path.unlink()
            self.assertEqual(ki.article_author_name(plan), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
