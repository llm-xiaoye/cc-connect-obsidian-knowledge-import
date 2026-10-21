from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANAGE = ROOT / "scripts/manage.py"

class PublicInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ki-public-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace with spaces"
        self.vault = self.root / "vault"
        self.workspace.mkdir()
        self.vault.mkdir()
        self.settings = self.workspace / ".claude/settings.json"

    def manage(self, command, *extra):
        args = [sys.executable, "-B", str(MANAGE), command, "--workspace", str(self.workspace)]
        if command == "install":
            args += ["--vault", str(self.vault)]
        return subprocess.run(args + list(extra), text=True, capture_output=True, timeout=30)

    def install(self, *extra):
        result = self.manage("install", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write_settings(self, value):
        self.settings.parent.mkdir(exist_ok=True)
        self.settings.write_text(json.dumps(value), encoding="utf-8")

    def test_hook_preserves_unrelated_settings_and_later_edits(self):
        original = {"env": {"EXAMPLE": "keep"}, "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo existing"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "keep this prompt"}]}]}}
        self.write_settings(original)
        self.install()
        installed = json.loads(self.settings.read_text())
        self.assertEqual(installed["env"], original["env"])
        self.assertEqual(installed["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        self.assertIn(original["hooks"]["UserPromptSubmit"][0], installed["hooks"]["UserPromptSubmit"])
        installed["permissions"] = {"allow": ["Read"]}
        self.write_settings(installed)
        checked = self.manage("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        removed = self.manage("uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        expected = {**original, "permissions": {"allow": ["Read"]}}
        self.assertEqual(json.loads(self.settings.read_text()), expected)

    def test_installed_hook_runs_with_spaces_and_real_stdin(self):
        self.install()
        settings = json.loads(self.settings.read_text())
        command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        result = subprocess.run(command, shell=True, input=json.dumps({
            "hook_event_name": "UserPromptSubmit", "prompt": "https://mp.weixin.qq.com/s/public-example"}),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        context = value["hookSpecificOutput"]["additionalContext"]
        self.assertIn("KI_RAW_ARGUMENTS_BEGIN\nhttps://mp.weixin.qq.com/s/public-example", context)
        self.assertIn(str(self.workspace / ".claude/skills/obsidian-knowledge-import/SKILL.md"), context)

    def test_bundled_modules_resolve_inside_installation(self):
        self.install()
        scripts = self.workspace / ".claude/skills/obsidian-knowledge-import/scripts"
        env = {**os.environ, "PYTHONPATH": str(scripts)}
        source = "import json,unified_store,unified_catalog,topic_taxonomy;print(json.dumps([m.__file__ for m in (unified_store,unified_catalog,topic_taxonomy)]))"
        result = subprocess.run([sys.executable, "-B", "-c", source], cwd=self.workspace,
            env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(Path(path).parent == scripts for path in paths))

    def test_nonempty_unmigrated_vault_is_not_modified(self):
        note = self.vault / "existing.md"
        note.write_text("keep", encoding="utf-8")
        result = self.manage("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(note.read_text(), "keep")
        self.assertFalse((self.workspace / ".claude").exists())
        self.assertEqual({p.name for p in self.vault.iterdir()}, {"existing.md"})

    def test_explicit_legacy_layout_keeps_existing_vault(self):
        (self.vault / "existing.md").write_text("keep", encoding="utf-8")
        self.install("--legacy-layout")
        config = json.loads((self.workspace / ".claude/ki-config.json").read_text())
        self.assertFalse(config["unified_only"])
        self.assertNotIn("notes_root", config)
        self.assertEqual({p.name for p in self.vault.iterdir()}, {"existing.md"})

    def test_invalid_python_fails_before_workspace_changes(self):
        result = self.manage("install", "--python", str(self.root / "missing-python"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.workspace / ".claude").exists())

    def test_upgrade_uninstall_restores_previous_hook(self):
        self.install()
        first = json.loads(self.settings.read_text())
        self.install()
        self.assertEqual(self.manage("uninstall").returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text()), first)
        checked = self.manage("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(self.manage("uninstall").returncode, 0)
        self.assertFalse(self.settings.exists())

    def test_invalid_hook_shape_does_not_replace_existing_files(self):
        self.write_settings({"hooks": {"UserPromptSubmit": "invalid"}})
        before = self.settings.read_bytes()
        result = self.manage("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.workspace / ".claude/commands/ki.md").exists())

if __name__ == "__main__":
    unittest.main()
