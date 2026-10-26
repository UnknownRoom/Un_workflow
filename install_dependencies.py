#!/usr/bin/env python3
"""Detect common dependency manifests in a checked-out repository and install them.

Usage:
    python3 install_dependencies.py [repository_path] [--dry-run]

The script reads manifests from the repository root. It uses the ecosystem's
normal package manager, so language standard libraries are not installed as
third-party packages. Python requirements files also get an explicit filter
for entries whose package name is a Python standard-library module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

STATE_FILE = ".dependency_installer_state.json"


def executable(name: str) -> str | None:
    return shutil.which(name)


def run_install(label: str, command: Sequence[str], root: Path, dry_run: bool, action: str = "install") -> bool:
    """Run an installer command, returning whether it completed successfully."""
    if not executable(command[0]):
        print(f"[skip] {label}: command not found: {command[0]}")
        return False

    print(f"[{action}] {label}: {shlex.join(command)}")
    if dry_run:
        return True

    try:
        result = subprocess.run(command, cwd=root, check=False)
    except OSError as exc:
        print(f"[error] {label}: {exc}")
        return False
    if result.returncode:
        print(f"[error] {label}: exited with status {result.returncode}")
        return False
    return True


def requirement_is_stdlib(line: str) -> bool:
    """Recognize a plain requirement whose normalized name is a stdlib module."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "-", "http://", "https://", "git+")):
        return False

    # Match ordinary PEP 508 names with optional extras/version/environment
    # markers. Leave URLs, editable installs, and other pip directives intact.
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?\s*(?:[<>=!~;\s]|$)", stripped)
    if not match:
        return False

    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    stdlib_names = {re.sub(r"[-_.]+", "-", item).lower() for item in sys.stdlib_module_names}
    return name in stdlib_names


def pip_install_requirements(requirement_file: Path, root: Path, dry_run: bool) -> bool:
    """Install a requirements file, filtering direct stdlib-name entries."""
    python = sys.executable
    if not executable(python):
        print(f"[skip] {requirement_file.name}: Python executable is unavailable: {python}")
        return False

    try:
        content = requirement_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"[error] cannot read {requirement_file}: {exc}")
        return False

    filtered = [line for line in content if not requirement_is_stdlib(line)]
    removed = len(content) - len(filtered)
    if removed:
        print(f"[info] {requirement_file.name}: excluded {removed} standard-library entr{'y' if removed == 1 else 'ies'}")

    if filtered == content:
        command = [python, "-m", "pip", "install", "-r", str(requirement_file)]
        return run_install(f"Python ({requirement_file.name})", command, root, dry_run)

    if dry_run:
        print(f"[install] Python ({requirement_file.name}): {shlex.join([python, '-m', 'pip', 'install', '-r', str(requirement_file)])} [stdlib entries filtered]")
        return True

    # Keep the temporary file next to its source so relative -r/-c paths keep
    # their usual base directory. It is removed even when pip fails.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".codex-requirements-",
            suffix=".txt", dir=requirement_file.parent, delete=False,
        ) as temporary:
            temporary.write("\n".join(filtered) + "\n")
            temp_path = Path(temporary.name)
        command = [python, "-m", "pip", "install", "-r", str(temp_path)]
        return run_install(f"Python ({requirement_file.name})", command, root, dry_run)
    except OSError as exc:
        print(f"[error] could not prepare filtered requirements: {exc}")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def detect_installs(root: Path) -> list[tuple[str, list[str]]]:
    """Return at most one preferred installer per ecosystem, in stable order."""
    installs: list[tuple[str, list[str]]] = []

    # Python: requirements files take precedence; otherwise use project/lock files.
    requirements = sorted(root.glob("requirements*.txt"))
    if requirements:
        # requirements are handled separately to apply the stdlib filter.
        pass
    elif (root / "Pipfile.lock").exists() or (root / "Pipfile").exists():
        if executable("pipenv"):
            installs.append(("Python (Pipenv)", ["pipenv", "sync" if (root / "Pipfile.lock").exists() else "install"]))
        else:
            print("[skip] Python (Pipenv): install pipenv to use Pipfile")
    elif (root / "poetry.lock").exists() and (root / "pyproject.toml").exists():
        if executable("poetry"):
            installs.append(("Python (Poetry)", ["poetry", "install", "--no-interaction"]))
        else:
            print("[skip] Python (Poetry): install poetry to use poetry.lock")
    elif (root / "uv.lock").exists() and (root / "pyproject.toml").exists():
        if executable("uv"):
            installs.append(("Python (uv)", ["uv", "sync"]))
        else:
            print("[skip] Python (uv): install uv to use uv.lock")
    elif (root / "pyproject.toml").exists():
        installs.append(("Python (project)", [sys.executable, "-m", "pip", "install", "."]))

    # JavaScript/TypeScript: prefer the lockfile's package manager.
    if (root / "package.json").exists():
        if (root / "pnpm-lock.yaml").exists():
            installs.append(("Node.js (pnpm)", ["pnpm", "install", "--frozen-lockfile"]))
        elif (root / "yarn.lock").exists():
            installs.append(("Node.js (Yarn)", ["yarn", "install", "--frozen-lockfile"]))
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            installs.append(("Node.js (Bun)", ["bun", "install", "--frozen-lockfile"]))
        elif (root / "package-lock.json").exists() or (root / "npm-shrinkwrap.json").exists():
            installs.append(("Node.js (npm)", ["npm", "ci"]))
        else:
            installs.append(("Node.js (npm)", ["npm", "install"]))

    # Go, Rust, Java, Ruby, PHP, .NET, Elixir, and Swift.
    if (root / "go.mod").exists():
        installs.append(("Go", ["go", "mod", "download"]))
    if (root / "Cargo.toml").exists():
        installs.append(("Rust", ["cargo", "fetch"]))
    if (root / "pom.xml").exists():
        wrapper = root / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        installs.append(("Java (Maven)", [str(wrapper), "-B", "-DskipTests", "dependency:go-offline"] if wrapper.exists() else ["mvn", "-B", "-DskipTests", "dependency:go-offline"]))
    elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
        installs.append(("Java (Gradle)", [str(wrapper), "--no-daemon", "dependencies"] if wrapper.exists() else ["gradle", "--no-daemon", "dependencies"]))
    if (root / "Gemfile").exists():
        installs.append(("Ruby", ["bundle", "install"]))
    if (root / "composer.json").exists():
        installs.append(("PHP", ["composer", "install", "--no-interaction"]))
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")):
        installs.append((".NET", ["dotnet", "restore"]))
    if (root / "mix.exs").exists():
        installs.append(("Elixir", ["mix", "deps.get"]))
    if (root / "Package.swift").exists():
        installs.append(("Swift", ["swift", "package", "resolve"]))

    return installs


def pip_packages() -> dict[str, str]:
    """Return installed distribution names and versions for this interpreter."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return {
                item["name"].lower().replace("_", "-"): item["version"]
                for item in json.loads(result.stdout)
            }
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return {}


def load_state(root: Path) -> dict:
    state_path = root / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"python": {}, "directories": []}


def save_state(root: Path, state: dict) -> bool:
    state_path = root / STATE_FILE
    try:
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[error] could not save install record {state_path}: {exc}")
        return False


def node_modules_entries(root: Path) -> set[str]:
    """Snapshot package-manager entries without walking package contents."""
    entries: set[str] = set()
    for current, dirs, _files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in {".git", ".venv", "venv", "vendor", "target"}]
        if Path(current).name != "node_modules":
            continue

        modules_dir = Path(current)
        entries.add(modules_dir.relative_to(root).as_posix())
        dirs[:] = []  # Do not recurse into installed packages.
        try:
            children = list(modules_dir.iterdir())
        except OSError:
            continue
        for child in children:
            entries.add(child.relative_to(root).as_posix())
            if child.name in {".pnpm", ".bin"} and child.is_dir():
                try:
                    entries.update(item.relative_to(root).as_posix() for item in child.iterdir())
                except OSError:
                    pass
    return entries


def uninstall(root: Path, dry_run: bool) -> int:
    state_path = root / STATE_FILE
    if not state_path.exists():
        print(f"[info] no install record found in {root}; run install mode first")
        return 0

    state = load_state(root)
    packages = state.get("python", {})
    if not isinstance(packages, dict):
        packages = {}

    current = pip_packages()
    has_tracked_artifacts = False
    remove_packages = [
        name for name, installed_version in packages.items()
        if current.get(name.lower().replace("_", "-")) == installed_version
    ]
    if remove_packages:
        has_tracked_artifacts = True
        command = [sys.executable, "-m", "pip", "uninstall", "-y", *sorted(remove_packages)]
        if dry_run:
            print(f"[uninstall] Python packages: {shlex.join(command)}")
        else:
            if not run_install("Python packages installed by this script", command, root, False, action="uninstall"):
                return 1
    elif packages:
        print("[info] recorded Python packages are no longer installed at their recorded versions; keeping them")

    directories = state.get("directories", [])
    if not isinstance(directories, list):
        directories = []
    for relative in directories:
        path = (root / str(relative)).resolve()
        # Only remove recorded paths inside the repository, and never the root itself.
        if path == root or root not in path.parents:
            print(f"[skip] unsafe recorded path: {relative}")
            continue
        if not path.exists():
            continue
        if dry_run:
            print(f"[uninstall] remove script-created directory: {path}")
            has_tracked_artifacts = True
        elif path.is_dir():
            try:
                shutil.rmtree(path)
                print(f"[uninstall] removed script-created directory: {path}")
                has_tracked_artifacts = True
            except OSError as exc:
                print(f"[error] could not remove {path}: {exc}")
                return 1
        else:
            print(f"[skip] recorded path is not a directory: {path}")

    entries = state.get("node_modules_entries", [])
    if not isinstance(entries, list):
        entries = []
    # Remove deepest entries first so newly-created package directories can be
    # removed cleanly after their children. Existing entries were never saved.
    safe_paths: list[Path] = []
    for relative in entries:
        # Use a lexical absolute path so a recorded symlink is unlinked itself,
        # rather than following it into its package target.
        path = Path(os.path.abspath(root / str(relative)))
        if path == root or root not in path.parents:
            print(f"[skip] unsafe recorded path: {relative}")
            continue
        safe_paths.append(path)
    for path in sorted(set(safe_paths), key=lambda item: len(item.parts), reverse=True):
        if not path.exists() and not path.is_symlink():
            continue
        if dry_run:
            print(f"[uninstall] remove script-added dependency entry: {path}")
            has_tracked_artifacts = True
        else:
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                print(f"[uninstall] removed script-added dependency entry: {path}")
                has_tracked_artifacts = True
            except OSError as exc:
                print(f"[error] could not remove {path}: {exc}")
                return 1

    if dry_run:
        print(f"[info] dry-run: would remove the install record {state_path}")
    else:
        try:
            state_path.unlink()
        except OSError as exc:
            print(f"[error] could not remove install record: {exc}")
            return 1
    if state.get("unmanaged"):
        print("[info] shared package-manager caches are kept because they may be used by other projects")
    if not has_tracked_artifacts:
        print("[info] no locally tracked dependencies were removed; this install record contains no removable artifacts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect and install repository dependencies.")
    parser.add_argument("repository", nargs="?", default=".", help="repository root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="show install commands without running them")
    parser.add_argument("--uninstall", action="store_true", help="remove dependencies recorded as installed by this script")
    args = parser.parse_args()

    root = Path(args.repository).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"repository path is not a directory: {root}")

    if args.uninstall:
        return uninstall(root, args.dry_run)

    requirements = sorted(root.glob("requirements*.txt"))
    installs = detect_installs(root)
    if not requirements and not installs:
        print(f"[info] no supported dependency manifests found in {root}")
        return 0

    state = load_state(root)
    recorded_packages = state.setdefault("python", {})
    if not isinstance(recorded_packages, dict):
        recorded_packages = {}
        state["python"] = recorded_packages
    packages_before = pip_packages() if not args.dry_run else {}
    node_entries_before = node_modules_entries(root) if not args.dry_run and (root / "package.json").exists() else set()
    candidates = []
    if (root / "package.json").exists():
        candidates.append(root / "node_modules")
    if (root / "composer.json").exists():
        candidates.append(root / "vendor")
    directories_before = {path for path in candidates if path.exists()}

    success = True
    for requirement_file in requirements:
        success = pip_install_requirements(requirement_file, root, args.dry_run) and success
    for label, command in installs:
        success = run_install(label, command, root, args.dry_run) and success

    if not args.dry_run:
        packages_after = pip_packages()
        for name, version in packages_after.items():
            if name not in packages_before:
                recorded_packages[name] = version
            elif name in recorded_packages and packages_before[name] == recorded_packages[name]:
                # Keep tracking an already-owned package if this run updated it.
                recorded_packages[name] = version
        recorded_directories = set(state.get("directories", []))
        for path in candidates:
            if path not in directories_before and path.exists():
                recorded_directories.add(path.relative_to(root).as_posix())
        state["directories"] = sorted(recorded_directories)
        if (root / "package.json").exists():
            node_entries_after = node_modules_entries(root)
            recorded_entries = set(state.get("node_modules_entries", []))
            recorded_entries.update(node_entries_after - node_entries_before)
            state["node_modules_entries"] = sorted(recorded_entries)
        state["unmanaged"] = sorted({
            label for label, _ in installs
            if label.startswith(("Node.js", "PHP", "Go", "Rust", "Java", "Ruby", ".NET", "Elixir", "Swift"))
            or label.startswith("Python (") and "requirements" not in label and "project" not in label
        })
        save_state(root, state)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
