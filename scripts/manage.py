#!/usr/bin/env python3
"""Install, verify, or uninstall the public /ki package in a cc-connect workspace."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
MANIFEST_REL = Path(".claude/obsidian-knowledge-import.install.json")
CONFIG_REL = Path(".claude/ki-config.json")
SETTINGS_REL = Path(".claude/settings.json")
BACKUP_BASE_REL = Path(".claude/backups/obsidian-knowledge-import")
PACKAGE_TARGETS: Tuple[Tuple[Path, Path], ...] = (
    (Path("command/ki.md"), Path(".claude/commands/ki.md")),
    (Path("command/knowledge-import.md"), Path(".claude/commands/knowledge-import.md")),
    (Path("command/导入.md"), Path(".claude/commands/导入.md")),
    (
        Path("skill/obsidian-knowledge-import"),
        Path(".claude/skills/obsidian-knowledge-import"),
    ),
)


class ManageError(Exception):
    """A concise, user-facing package management error."""


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_existing_dir(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ManageError(f"{label} 不存在：{path}") from exc
    if not resolved.is_dir():
        raise ManageError(f"{label} 不是目录：{resolved}")
    return resolved


def ensure_under_workspace(workspace: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ManageError(f"内部目标路径不安全：{relative}")
    target = workspace / relative
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ManageError(f"目标路径越出 workspace：{target}") from exc
    current = target
    while current != workspace:
        if current.is_symlink():
            raise ManageError(f"为避免改写意外位置，拒绝符号链接路径：{current}")
        current = current.parent
    return target


def reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ManageError(f"为避免越界写入，拒绝符号链接目标：{path}")
    if path.exists() and path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise ManageError(f"为避免越界写入，拒绝包含符号链接的目标：{child}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_digest(path: Path) -> str:
    reject_symlink(path)
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        raise ManageError(f"无法计算目标摘要：{path}")
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(child).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def remove_target(path: Path) -> None:
    reject_symlink(path)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def copy_source(source: Path, target: Path) -> None:
    if not source.exists():
        raise ManageError(f"发布包不完整，缺少：{source.relative_to(REPO_ROOT)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
        )
    else:
        shutil.copy2(source, target)


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def read_manifest(workspace: Path) -> Dict[str, Any]:
    path = ensure_under_workspace(workspace, MANIFEST_REL)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManageError("未找到安装清单；请先运行 install") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManageError(f"安装清单损坏：{exc}") from exc
    if not isinstance(value, dict) or value.get("package") != "obsidian-knowledge-import":
        raise ManageError("安装清单格式无效")
    return value



def selected_python(raw):
    path = Path(raw).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ManageError("Python 解释器不存在或不可执行")
    result = subprocess.run([str(path), "-B", "-c",
        "import sys; print('.'.join(map(str,sys.version_info[:3]))); sys.exit(0 if sys.version_info >= (3,9) else 2)"],
        capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise ManageError("需要可运行的 Python 3.9 或更高版本")
    return str(path)

def load_settings(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ManageError("settings.json 的根和 hooks 必须为 object")
    groups = value.get("hooks", {}).get("UserPromptSubmit", [])
    if not isinstance(groups, list):
        raise ManageError("UserPromptSubmit 必须为 array")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
            raise ManageError("UserPromptSubmit hook 格式无效")
        if any(not isinstance(hook, dict) for hook in group.get("hooks", [])):
            raise ManageError("hook 条目必须为 object")
    return value

def change_hook(settings, remove_command=None, add_command=None):
    hooks = dict(settings.get("hooks", {}))
    removable = {value for value in (remove_command, add_command) if value is not None}
    groups = []
    for original in hooks.get("UserPromptSubmit", []):
        group = dict(original)
        entries = [dict(item) for item in group.get("hooks", [])
                   if item.get("command") not in removable]
        if entries or not group.get("hooks"):
            group["hooks"] = entries
            groups.append(group)
    if add_command:
        groups.append({"hooks": [{"type": "command", "command": add_command}]})
    if groups:
        hooks["UserPromptSubmit"] = groups
    else:
        hooks.pop("UserPromptSubmit", None)
    result = dict(settings)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result

def catalog_preparation(vault, legacy):
    if legacy:
        return {}
    import importlib.util
    import sys as runtime_sys
    scripts = REPO_ROOT / "skill/obsidian-knowledge-import/scripts"
    runtime_sys.path.insert(0, str(scripts))
    try:
        import unified_store
        paths = (vault / unified_store.INDEX_NAME, vault / unified_store.LOG_NAME)
        if all(path.is_file() and not path.is_symlink() for path in paths):
            unified_store.load(vault)
            return {}
        if any(path.exists() or path.is_symlink() for path in paths):
            raise ManageError("统一总览不完整或不安全，未修改 Vault")
        if any(path.name not in {".obsidian", ".DS_Store"} for path in vault.iterdir()):
            raise ManageError("非空 Vault 缺少统一总览；旧版布局请使用 --legacy-layout，不自动迁移笔记")
        return {paths[0]: unified_store.render_index([]), paths[1]: unified_store.render_log([])}
    finally:
        runtime_sys.path.remove(str(scripts))


def render_installed_text(text, python, legacy):
    text = text.replace("@KI_PYTHON@", shlex.quote(python))
    if not legacy:
        return text
    lines = []
    for line in text.splitlines():
        if line.startswith("- Vault 优先级："):
            line = "- Vault 优先级：KI_VAULT → 工作区 .claude/ki-config.json → ~/Documents/obsidian。当前为旧版布局，笔记直接写入主题目录，维护 _import-log.md 和 _knowledge-index.md。"
        elif line.startswith("- 两份总览是") or line.startswith("- " + chr(96) + "导入总日志.md"):
            continue
        elif line.startswith("新建 " + chr(96) + "relative_path"):
            line = "新建 relative_path 是 Vault 内相对 .md 路径，top_level/second_level 对应路径前两级；不添加统一目录前缀。提交器生成 frontmatter，文件冲突时使用 -2 后缀。"
        line = line.replace("路径必须以 " + chr(96) + "知识库/" + chr(96) + " 开头，是 Vault 内相对", "路径必须是 Vault 内相对")
        lines.append(line.replace("知识库/", ""))
    return "\n".join(lines) + "\n"

def staged_package(stage_root, vault, workspace, python, legacy):
    installed = []
    for source_rel, target_rel in PACKAGE_TARGETS:
        copy_source(REPO_ROOT / source_rel, stage_root / target_rel)
        installed.append(target_rel)
    skill = stage_root / ".claude/skills/obsidian-knowledge-import"
    for path in (skill / "SKILL.md", skill / "references/analysis-schema.md", skill / "references/note-schema.md"):
        path.write_text(render_installed_text(path.read_text(encoding="utf-8"), python, legacy), encoding="utf-8")
    config_path = ensure_under_workspace(workspace, CONFIG_REL)
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    if not isinstance(config, dict):
        raise ManageError("ki-config.json 必须为 object")
    config["vault"] = str(vault)
    if legacy:
        config.pop("notes_root", None)
        config["unified_only"] = False
    else:
        config["notes_root"] = "知识库"
        config["unified_only"] = True
    write_json(stage_root / CONFIG_REL, config)
    installed.append(CONFIG_REL)
    digests = {relative.as_posix(): target_digest(stage_root / relative) for relative in installed}
    settings_path = ensure_under_workspace(workspace, SETTINGS_REL)
    settings = load_settings(settings_path)
    previous_manifest_path = ensure_under_workspace(workspace, MANIFEST_REL)
    previous = read_manifest(workspace) if previous_manifest_path.is_file() else {}
    previous_hook = previous.get("hook", {}).get("command")
    command = " ".join(shlex.quote(part) for part in
        (python, "-B", str(workspace / ".claude/skills/obsidian-knowledge-import/scripts/auto_route.py")))
    write_json(stage_root / SETTINGS_REL, change_hook(settings, previous_hook, command))
    installed.append(SETTINGS_REL)
    hook = {"command": command, "settings_existed": settings_path.exists()}
    return installed, digests, hook


def install(args: argparse.Namespace) -> None:
    workspace = resolve_existing_dir(args.workspace, "workspace")
    vault = resolve_existing_dir(args.vault, "Obsidian Vault")
    python = selected_python(args.python)
    catalog_files = catalog_preparation(vault, args.legacy_layout)
    created_catalogs = []
    if platform.system() == "Windows":
        raise ManageError("当前版本依赖 Unix 文件锁；Windows 请在 WSL 中安装")
    if sys.version_info < (3, 9):
        raise ManageError("需要 Python 3.9 或更高版本")

    claude_dir = workspace / ".claude"
    if claude_dir.is_symlink():
        raise ManageError(f"为避免越界安装，拒绝符号链接目录：{claude_dir}")
    claude_dir.mkdir(parents=True, exist_ok=True)
    backup_rel = BACKUP_BASE_REL / f"{utc_stamp()}-{uuid.uuid4().hex[:8]}"
    backup_root = ensure_under_workspace(workspace, backup_rel)
    stage_root = Path(tempfile.mkdtemp(prefix=".ki-install-", dir=str(claude_dir)))
    installed_now: List[Path] = []
    moved_backups: List[Path] = []

    try:
        installed, digests, hook = staged_package(stage_root, vault, workspace, python, args.legacy_layout)
        all_targets = installed + [MANIFEST_REL]
        backup_map: Dict[str, str] = {}
        for relative in all_targets:
            destination = ensure_under_workspace(workspace, relative)
            reject_symlink(destination)
            if destination.exists():
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                moved_backups.append(relative)
                backup_map[relative.as_posix()] = (backup_rel / relative).as_posix()

        manifest: Dict[str, Any] = {
            "schema": 1,
            "package": "obsidian-knowledge-import",
            "version": VERSION,
            "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "workspace": str(workspace),
            "vault": str(vault),
            "targets": digests,
            "python": python,
            "hook": hook,
            "backup": backup_map,
            "backup_root": backup_rel.as_posix() if backup_map else None,
        }
        write_json(stage_root / MANIFEST_REL, manifest)

        for relative in installed + [MANIFEST_REL]:
            source = stage_root / relative
            destination = ensure_under_workspace(workspace, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)

            installed_now.append(relative)
        for path, content in catalog_files.items():
            with path.open("xb") as stream:
                created_catalogs.append(path)
                stream.write(content)
            path.chmod(0o600)
    except Exception:
        for path in reversed(created_catalogs):
            path.unlink()
        for relative in reversed(installed_now):
            destination = ensure_under_workspace(workspace, relative)
            if destination.exists():
                remove_target(destination)
        for relative in reversed(moved_backups):
            backup = backup_root / relative
            destination = ensure_under_workspace(workspace, relative)
            if backup.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
        raise
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)

    print(f"✅ 已安装 obsidian-knowledge-import {VERSION}")
    print(f"   workspace: {workspace}")
    print(f"   Vault:     {vault}")
    print("   命令:      /ki、/knowledge-import、/导入")
    print(f"下一步：python3 {Path(__file__).resolve()} check --workspace {workspace}")


def compile_python(path: Path) -> None:
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
    except (OSError, SyntaxError) as exc:
        raise ManageError(f"Python 脚本校验失败：{exc}") from exc


def runtime_version(command: str) -> Optional[str]:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "已安装（版本读取失败）"
    return (result.stdout or result.stderr).strip().splitlines()[0]


def check(args: argparse.Namespace) -> None:
    workspace = resolve_existing_dir(args.workspace, "workspace")
    manifest = read_manifest(workspace)
    if manifest.get("workspace") != str(workspace):
        raise ManageError("安装清单所属 workspace 与当前目录不一致")

    targets = manifest.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise ManageError("安装清单缺少 targets")
    for raw_relative, expected in targets.items():
        relative = Path(raw_relative)
        target = ensure_under_workspace(workspace, relative)
        if not target.exists():
            raise ManageError(f"安装文件缺失：{relative}")
        actual = target_digest(target)
        if actual != expected:
            raise ManageError(f"安装文件已被修改：{relative}")

    config_path = ensure_under_workspace(workspace, CONFIG_REL)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManageError(f"Vault 配置无效：{exc}") from exc
    vault_raw = config.get("vault") if isinstance(config, dict) else None
    if not isinstance(vault_raw, str) or not vault_raw:
        raise ManageError("Vault 配置缺少 vault")
    vault = resolve_existing_dir(vault_raw, "Obsidian Vault")
    if not os.access(vault, os.R_OK | os.W_OK | os.X_OK):
        raise ManageError(f"Obsidian Vault 不可读写：{vault}")

    script = workspace / ".claude/skills/obsidian-knowledge-import/scripts/ki_import.py"

    for member in script.parent.glob("*.py"):
        compile_python(member)
    python = selected_python(manifest.get("python", sys.executable))
    settings = load_settings(ensure_under_workspace(workspace, SETTINGS_REL))
    expected_hook = manifest.get("hook", {}).get("command")
    observed_hooks = [item.get("command") for group in settings.get("hooks", {}).get("UserPromptSubmit", [])
                      for item in group.get("hooks", [])]
    if expected_hook and observed_hooks.count(expected_hook) != 1:
        raise ManageError("自动路由 hook 缺失或重复")

    with tempfile.TemporaryDirectory(prefix="ki-check-") as temp:
        child_env = {**os.environ, "TMPDIR": temp, "PYTHONDONTWRITEBYTECODE": "1"}
        initialized = subprocess.run([python, "-B", str(script), "init"], cwd=str(workspace),
            capture_output=True, text=True, timeout=20, env=child_env, check=False)
        if initialized.returncode:
            raise ManageError("工作流初始化自检失败")
        artifacts = json.loads(initialized.stdout)["artifacts"]
        raw_file = Path(artifacts["raw_file"])
        raw_file.write_text("not-a-url\n", encoding="utf-8")
        result = subprocess.run(
            [python, "-B", str(script), "plan", "--input-file", str(raw_file),
             "--output", artifacts["plan_file"]],
            cwd=str(workspace), capture_output=True, text=True, timeout=20, env=child_env, check=False)
        if result.returncode != 0 or not Path(artifacts["plan_file"]).is_file():
            raise ManageError("工作流参数校验自检失败")
        plan = json.loads(Path(artifacts["plan_file"]).read_text(encoding="utf-8"))
        if plan.get("code") != "no-valid-url":
            raise ManageError("工作流自检返回了非预期结果")

    print(f"✅ 安装完整性通过：{manifest.get('version', 'unknown')}")
    print(f"✅ Vault 可读写：{vault}")
    print(f"✅ Python：{platform.python_version()}")
    curl_version = runtime_version("curl")
    if not curl_version:
        raise ManageError("未在 PATH 中找到必需的 curl")
    print(f"✅ curl：{curl_version}")
    for command in ("claude", "cc-connect"):
        version = runtime_version(command)
        if version:
            print(f"✅ {command}：{version}")
        else:
            print(f"⚠️  未在 PATH 中找到 {command}")
    print("自检没有写入 Vault。若 cc-connect 正在运行，请重启它以刷新命令。")


def validated_backup_map(
    workspace: Path, manifest: Dict[str, Any]
) -> Tuple[Dict[Path, Path], Optional[Path]]:
    raw_map = manifest.get("backup", {})
    if not isinstance(raw_map, dict):
        raise ManageError("安装清单的 backup 字段无效")
    raw_root = manifest.get("backup_root")
    if raw_root is None and not raw_map:
        backup_root = None
    elif not isinstance(raw_root, str):
        raise ManageError("安装清单的 backup_root 字段无效")
    else:
        backup_root = ensure_under_workspace(workspace, Path(raw_root))
        try:
            backup_root.relative_to(ensure_under_workspace(workspace, BACKUP_BASE_REL))
        except ValueError as exc:
            raise ManageError(f"备份根目录越界：{backup_root}") from exc

    result: Dict[Path, Path] = {}
    for raw_target, raw_backup in raw_map.items():
        if not isinstance(raw_target, str) or not isinstance(raw_backup, str):
            raise ManageError("安装清单包含无效备份路径")
        target_rel = Path(raw_target)
        backup_rel = Path(raw_backup)
        target = ensure_under_workspace(workspace, target_rel)
        backup = ensure_under_workspace(workspace, backup_rel)
        if backup_root is None:
            raise ManageError("安装清单有备份条目但缺少 backup_root")
        try:
            backup.relative_to(backup_root)
        except ValueError as exc:
            raise ManageError(f"备份路径不属于本次安装：{backup}") from exc
        result[target_rel] = backup
    return result, backup_root


def uninstall(args: argparse.Namespace) -> None:
    workspace = resolve_existing_dir(args.workspace, "workspace")
    manifest = read_manifest(workspace)
    if manifest.get("workspace") != str(workspace):
        raise ManageError("安装清单所属 workspace 与当前目录不一致")
    targets = manifest.get("targets")
    if not isinstance(targets, dict):
        raise ManageError("安装清单缺少 targets")

    changed: List[str] = []
    for raw_relative, expected in targets.items():
        target = ensure_under_workspace(workspace, Path(raw_relative))
        if not target.exists() or target_digest(target) != expected:
            changed.append(raw_relative)
    if changed and not args.force:
        joined = "、".join(changed)
        raise ManageError(f"这些安装文件已被修改，未删除：{joined}。确认丢弃修改时加 --force")

    backup_map, backup_root = validated_backup_map(workspace, manifest)
    for backup in backup_map.values():
        if not backup.exists():
            raise ManageError(f"备份缺失，未卸载：{backup}")

    settings_path = ensure_under_workspace(workspace, SETTINGS_REL)
    settings = load_settings(settings_path)
    previous_manifest = {}
    if MANIFEST_REL in backup_map:
        previous_manifest = json.loads(backup_map[MANIFEST_REL].read_text(encoding="utf-8"))
    previous_hook = previous_manifest.get("hook", {}).get("command")
    updated_settings = change_hook(settings, manifest.get("hook", {}).get("command"), previous_hook)
    if updated_settings or manifest.get("hook", {}).get("settings_existed", True):
        write_json(settings_path, updated_settings)
    elif settings_path.exists():
        settings_path.unlink()
    backup_map.pop(SETTINGS_REL, None)
    installed = [Path(raw) for raw in targets]
    for relative in installed:
        target = ensure_under_workspace(workspace, relative)
        if target.exists():
            remove_target(target)

    manifest_path = ensure_under_workspace(workspace, MANIFEST_REL)
    manifest_path.unlink()

    restore_order = list(backup_map.items())
    restore_order.sort(key=lambda item: item[0] == MANIFEST_REL)
    for relative, backup in restore_order:
        destination = ensure_under_workspace(workspace, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, destination)

    if backup_root and backup_root.exists():
        shutil.rmtree(backup_root)

    print("✅ 已卸载 obsidian-knowledge-import")
    if backup_map:
        print("✅ 已恢复安装前的同名文件")
    print("未删除 Obsidian Vault 中的任何笔记。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="安装或升级到 cc-connect workspace")
    install_parser.add_argument("--workspace", required=True, help="cc-connect 项目的 work_dir")
    install_parser.add_argument("--vault", required=True, help="现有 Obsidian Vault 目录")
    install_parser.add_argument("--python", default=sys.executable, help="本机 Python 的绝对路径；默认使用安装器解释器")
    install_parser.add_argument("--legacy-layout", action="store_true", help="继续使用旧版分离日志布局，不迁移已有笔记")
    install_parser.set_defaults(func=install)

    check_parser = subparsers.add_parser("check", help="只读检查安装与运行环境")
    check_parser.add_argument("--workspace", required=True, help="cc-connect 项目的 work_dir")
    check_parser.set_defaults(func=check)

    uninstall_parser = subparsers.add_parser("uninstall", help="卸载并恢复安装前文件")
    uninstall_parser.add_argument("--workspace", required=True, help="cc-connect 项目的 work_dir")
    uninstall_parser.add_argument("--force", action="store_true", help="丢弃已修改的安装文件")
    uninstall_parser.set_defaults(func=uninstall)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.func(args)
        return 0
    except ManageError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"❌ 操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
