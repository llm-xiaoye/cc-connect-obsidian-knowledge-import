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
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
MANIFEST_REL = Path(".claude/obsidian-knowledge-import.install.json")
CONFIG_REL = Path(".claude/ki-config.json")
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


def staged_package(stage_root: Path, vault: Path) -> Tuple[List[Path], Dict[str, str]]:
    installed: List[Path] = []
    for source_rel, target_rel in PACKAGE_TARGETS:
        copy_source(REPO_ROOT / source_rel, stage_root / target_rel)
        installed.append(target_rel)
    write_json(stage_root / CONFIG_REL, {"vault": str(vault)})
    installed.append(CONFIG_REL)
    digests = {relative.as_posix(): target_digest(stage_root / relative) for relative in installed}
    return installed, digests


def install(args: argparse.Namespace) -> None:
    workspace = resolve_existing_dir(args.workspace, "workspace")
    vault = resolve_existing_dir(args.vault, "Obsidian Vault")
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
        installed, digests = staged_package(stage_root, vault)
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
    except Exception:
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
    compile_python(script)
    with tempfile.TemporaryDirectory(prefix="ki-check-") as temp:
        raw_file = Path(temp) / "raw.txt"
        plan_file = Path(temp) / "plan.json"
        raw_file.write_text("not-a-url\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), "plan", "--input-file", str(raw_file), "--output", str(plan_file)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0 or not plan_file.is_file():
            raise ManageError(f"工作流自检失败：{result.stderr.strip() or result.stdout.strip()}")
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
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
