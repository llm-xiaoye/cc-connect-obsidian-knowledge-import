from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANAGE = ROOT / "scripts/manage.py"
COMMANDS = ("ki.md", "knowledge-import.md", "导入.md")


class ManageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.vault = self.root / "vault"
        self.workspace.mkdir()
        self.vault.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_manage(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MANAGE), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def install(self) -> subprocess.CompletedProcess[str]:
        return self.run_manage(
            "install",
            "--workspace",
            str(self.workspace),
            "--vault",
            str(self.vault),
        )

    def test_command_contract_prevents_nested_skill_argument_loss(self) -> None:
        url = "https://mp.weixin.qq.com/s/UZzha5NG4x6sei32Y338rg"
        for name in COMMANDS:
            command = (ROOT / "command" / name).read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", command)
            self.assertIn("禁止再次调用任何 Skill 工具", command)
            self.assertTrue(command.rstrip().endswith("KI_RAW_ARGUMENTS_BEGIN"))
            expanded = command + "\n" + url
            raw = expanded.rsplit("KI_RAW_ARGUMENTS_BEGIN", 1)[1].strip()
            self.assertEqual(raw, url)

        skill = (ROOT / "skill/obsidian-knowledge-import/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("最后一个同名标记之后", skill)
        self.assertIn("最后一个 `ARGUMENTS:` 标记之后", skill)
        self.assertIn("不得创建空参数文件", skill)

    def test_clean_install_and_check(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.workspace / ".claude/commands/ki.md").is_file())
        self.assertTrue((self.workspace / ".claude/commands/knowledge-import.md").is_file())
        self.assertTrue((self.workspace / ".claude/commands/导入.md").is_file())
        script = self.workspace / ".claude/skills/obsidian-knowledge-import/scripts/ki_import.py"
        self.assertTrue(script.is_file())
        self.assertFalse((script.parent / "__pycache__").exists())
        config = json.loads((self.workspace / ".claude/ki-config.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"vault": str(self.vault.resolve())})

        checked = self.run_manage("check", "--workspace", str(self.workspace))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("安装完整性通过", checked.stdout)
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_install_requires_existing_vault(self) -> None:
        result = self.run_manage(
            "install",
            "--workspace",
            str(self.workspace),
            "--vault",
            str(self.root / "missing"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.workspace / ".claude").exists())

    def test_install_rejects_symlinked_claude_directory(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / ".claude").symlink_to(outside, target_is_directory=True)
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_uninstall_restores_preexisting_files(self) -> None:
        command = self.workspace / ".claude/commands/ki.md"
        config = self.workspace / ".claude/ki-config.json"
        command.parent.mkdir(parents=True)
        command.write_text("original command\n", encoding="utf-8")
        config.write_text('{"custom": true}\n', encoding="utf-8")

        self.assertEqual(self.install().returncode, 0)
        result = self.run_manage("uninstall", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(command.read_text(encoding="utf-8"), "original command\n")
        self.assertEqual(config.read_text(encoding="utf-8"), '{"custom": true}\n')
        self.assertFalse((self.workspace / ".claude/commands/knowledge-import.md").exists())
        self.assertFalse((self.workspace / ".claude/skills/obsidian-knowledge-import").exists())

    def test_uninstall_refuses_modified_package_without_force(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        command = self.workspace / ".claude/commands/ki.md"
        command.write_text("user change\n", encoding="utf-8")

        refused = self.run_manage("uninstall", "--workspace", str(self.workspace))
        self.assertEqual(refused.returncode, 2)
        self.assertTrue(command.exists())

        forced = self.run_manage("uninstall", "--workspace", str(self.workspace), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse(command.exists())

    def test_reinstall_can_be_uninstalled_back_to_previous_version(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        original = (self.workspace / ".claude/commands/ki.md").read_text(encoding="utf-8")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(
            self.run_manage("uninstall", "--workspace", str(self.workspace)).returncode,
            0,
        )
        self.assertEqual(
            (self.workspace / ".claude/commands/ki.md").read_text(encoding="utf-8"),
            original,
        )
        self.assertTrue((self.workspace / ".claude/obsidian-knowledge-import.install.json").exists())

    def test_uninstall_only_removes_current_backup_root(self) -> None:
        unrelated = self.workspace / ".claude/backups/obsidian-knowledge-import/keep/marker.txt"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep\n", encoding="utf-8")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)

        result = self.run_manage("uninstall", "--workspace", str(self.workspace))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
