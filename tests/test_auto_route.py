"""Local-only regression checks: no fetch, timer, provider, or Vault writes."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from auto_route import hook_output
from ki_import import extract_urls, plan_from_raw


ONE = "https://mp.weixin.qq.com/s/article-one"
TWO = "https://mp.weixin.qq.com/s/article-two"
THREE = "https://mp.weixin.qq.com/s/article-three"


def route(prompt):
    return hook_output({"hook_event_name": "UserPromptSubmit", "prompt": prompt})


def arguments(output):
    return output["hookSpecificOutput"]["additionalContext"].rsplit("KI_RAW_ARGUMENTS_BEGIN\n", 1)[1]


class AutoRouteTests(unittest.TestCase):
    def test_single_link_routes_to_existing_skill(self):
        result = route(ONE)
        self.assertEqual(arguments(result), ONE)
        skill = Path(__file__).resolve().parents[1] / "skill/obsidian-knowledge-import/SKILL.md"
        self.assertIn(str(skill), result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(plan_from_raw(arguments(result))["current_url"], ONE)

    def test_three_space_separated_links_preserve_batch_order(self):
        result = route(ONE + " " + TWO + " " + THREE)
        plan = plan_from_raw(arguments(result))
        self.assertEqual(plan["mode"], "batch")
        self.assertEqual(plan["input_urls"], [ONE, TWO, THREE])

    def test_whitespace_and_duplicate_links(self):
        result = route(ONE + "\t" + TWO + "\n" + ONE + "\u3000" + THREE)
        self.assertEqual(plan_from_raw(arguments(result))["input_urls"], [ONE, TWO, THREE])

    def test_markdown_label_and_target_do_not_leak_delimiters(self):
        message = "[weixin.qq.com](" + ONE + ") [" + TWO + "](" + TWO + ")"
        self.assertEqual(extract_urls(message), [ONE, TWO])
        self.assertEqual(plan_from_raw(arguments(route(message)))["input_urls"], [ONE, TWO])

    def test_bare_wechat_links_and_chinese_punctuation(self):
        message = "mp.weixin.qq.com/s/article-one。 <" + TWO + "> （" + THREE + "）"
        self.assertEqual(plan_from_raw(arguments(route(message)))["input_urls"], [ONE, TWO, THREE])

    def test_query_link_retains_parameters_and_decodes_html_ampersand(self):
        raw = "https://mp.weixin.qq.com/s?__biz=MzABC%3D%3D&amp;mid=123&amp;idx=1&amp;sn=abc"
        expected = raw.replace("&amp;", "&")
        plan = plan_from_raw(arguments(route("[文章](" + raw + ")")))
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["current_url"], expected)
        self.assertEqual(plan_from_raw("https://mp.weixin.qq.com/s")["status"], "error")

    def test_mentions_and_fake_domains_do_not_route(self):
        for message in [
            "解释 weixin.qq.com", "https://weixin.qq.com.example.org/s/one",
            "https://example.org/?next=weixin.qq.com/s/one",
            "[weixin.qq.com](https://example.org/news)",
            "https://evilweixin.qq.com/s/one",
            "https://v.douyin.com/example/", "普通聊天"
        ]:
            with self.subTest(message=message):
                self.assertIsNone(route(message))

    def test_explicit_commands_and_expanded_payload_are_not_reentered(self):
        for prefix in ["/ki ", "/knowledge-import ", "/导入 ", "/ki --single ", "/ki --force "]:
            self.assertIsNone(route(prefix + ONE))
        self.assertIsNone(route("already expanded\nKI_RAW_ARGUMENTS_BEGIN\n" + ONE))
        self.assertIsNone(hook_output({"hook_event_name": "PostToolUse", "prompt": ONE}))

    def test_manual_batch_single_and_force_still_work(self):
        self.assertEqual(plan_from_raw(ONE + " " + TWO)["mode"], "batch")
        self.assertEqual(plan_from_raw("--single " + ONE)["input_urls"], [ONE])
        self.assertTrue(plan_from_raw("--force " + ONE)["force"])
        self.assertEqual(plan_from_raw("--single " + ONE + " " + TWO)["status"], "error")

    def test_generic_balanced_parentheses_ipv6_and_query_names_are_preserved(self):
        urls = ["https://example.org/Foo_(bar)", "https://[2606:4700:4700::1111]/news", "https://example.org/?copy=1&notit=2"]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(extract_urls("[标题](" + url + ")"), [url])

    def test_unrelated_links_are_not_added_to_automatic_wechat_batch(self):
        result = route("https://example.org/unrelated " + ONE + " " + TWO)
        self.assertEqual(plan_from_raw(arguments(result))["input_urls"], [ONE, TWO])

    def test_real_hook_stdin_stdout_contract(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "skill/obsidian-knowledge-import/scripts/auto_route.py")],
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": ONE + " " + TWO}),
            text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(plan_from_raw(arguments(json.loads(completed.stdout)))["input_urls"], [ONE, TWO])


if __name__ == "__main__":
    unittest.main(verbosity=2)
