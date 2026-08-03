import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def log(message: str) -> None:
    print(message, file=sys.stderr)


def package_file_for(source_directory: str) -> Path:
    if not source_directory.startswith("plugins"):
        raise ValueError(f"不支持的插件目录：{source_directory}")
    suffix = source_directory[len("plugins") :]
    return Path(f"package{suffix}.json")


def read_package(path: Path, revision: str | None = None) -> dict[str, Any]:
    if revision:
        try:
            content = subprocess.check_output(
                ["git", "show", f"{revision}:{path.as_posix()}"],
                text=True,
                encoding="utf-8",
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return {}
        return json.loads(content)
    return json.loads(path.read_text(encoding="utf-8"))


def source_version(plugin_directory: Path) -> str:
    version_file = plugin_directory / "version.py"
    candidates = (
        (version_file, "VERSION"),
        (plugin_directory / "__init__.py", "plugin_version"),
    )
    for path, variable in candidates:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == variable
                for target in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant):
                return str(node.value.value)
        raise ValueError(f"{path} 中未找到常量 {variable}")
    raise ValueError(f"{plugin_directory} 中未找到插件版本")


def build_metadata(
    plugin_id: str, source_directory: str, package_data: dict[str, Any]
) -> dict[str, Any]:
    info = package_data[plugin_id]
    version = str(info.get("version") or "").strip()
    if not version:
        raise ValueError(f"{plugin_id} 未声明 version")
    if info.get("release") is not True:
        raise ValueError(f"{plugin_id} 未声明 release: true")

    directory = plugin_id.lower()
    plugin_directory = Path(source_directory) / directory
    actual_version = source_version(plugin_directory)
    if actual_version != version:
        raise ValueError(
            f"{plugin_id} 版本不一致：package={version}，source={actual_version}"
        )

    frontend_candidates = (
        Path("frontend") / directory,
        plugin_directory,
    )
    frontend_directory = next(
        (
            candidate
            for candidate in frontend_candidates
            if (candidate / "package.json").is_file()
        ),
        None,
    )
    has_frontend = frontend_directory is not None
    notes = info.get("history", {}).get(f"v{version}", "")
    return {
        "id": plugin_id,
        "name": info.get("name") or plugin_id,
        "version": version,
        "notes": notes,
        "source_directory": source_directory,
        "directory": directory,
        "has_frontend": has_frontend,
        "frontend_directory": (
            frontend_directory.as_posix() if frontend_directory else ""
        ),
        "tag_name": f"{plugin_id}_v{version}",
        "asset_name": f"{directory}_v{version}.zip",
    }


def manual_plugins() -> list[dict[str, Any]]:
    plugin_id = os.environ.get("INPUT_PLUGIN_ID", "").strip()
    source_directory = os.environ.get("INPUT_SOURCE_DIRECTORY", "").strip()
    if not plugin_id or not source_directory:
        raise ValueError("手动发布必须指定插件 ID 和源码目录")

    package_file = package_file_for(source_directory)
    package_data = read_package(package_file)
    if plugin_id not in package_data:
        raise ValueError(f"{package_file} 中不存在插件 {plugin_id}")
    return [build_metadata(plugin_id, source_directory, package_data)]


def changed_package_files(before: str, after: str) -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "diff", "--name-only", before, after],
            text=True,
            encoding="utf-8",
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        # 强推或改写历史后，GitHub 事件中的 before 可能不在 checkout 中。
        # 工作流仅由 package*.json 触发，此时重新检查当前清单即可安全恢复发布。
        detail = (error.stderr or "").strip()
        log(f"[Warn] 无法读取提交范围 {before}..{after}，改为检查当前插件清单：{detail}")
        return sorted(Path.cwd().glob("package*.json"))
    return [
        Path(name)
        for name in output.splitlines()
        if name.startswith("package") and name.endswith(".json") and "/" not in name
    ]


def automatic_plugins() -> list[dict[str, Any]]:
    before = os.environ.get("BEFORE_SHA", "").strip()
    after = os.environ.get("AFTER_SHA", "").strip()
    if not before or not after:
        raise ValueError("自动发布缺少提交范围")
    if set(before) == {"0"}:
        before = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    result: list[dict[str, Any]] = []
    for package_file in changed_package_files(before, after):
        suffix = package_file.stem[len("package") :]
        source_directory = f"plugins{suffix}"
        old_package = read_package(package_file, before)
        new_package = read_package(package_file)

        for plugin_id, new_info in new_package.items():
            old_info = old_package.get(plugin_id, {})
            release_enabled = (
                old_info.get("release") is not True
                and new_info.get("release") is True
            )
            version_changed = old_info.get("version") != new_info.get("version")
            newly_added = plugin_id not in old_package
            if not (release_enabled or version_changed or newly_added):
                continue
            if new_info.get("release") is not True:
                continue
            result.append(build_metadata(plugin_id, source_directory, new_package))

    return result


def main() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        plugins = manual_plugins()
    elif event_name == "push":
        plugins = automatic_plugins()
    else:
        raise ValueError(f"不支持的触发方式：{event_name}")
    print(json.dumps(plugins, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"[Fatal] {error}")
        raise SystemExit(1) from error
