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
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


def executable(name: str) -> str | None:
    return shutil.which(name)


def run_install(label: str, command: Sequence[str], root: Path, dry_run: bool) -> bool:
    """Run an installer command, returning whether it completed successfully."""
    if not executable(command[0]):
        print(f"[skip] {label}: command not found: {command[0]}")
        return False

    print(f"[install] {label}: {shlex.join(command)}")
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
        command = ["python3", "-m", "pip", "install", "-r", str(temp_path)]
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect and install repository dependencies.")
    parser.add_argument("repository", nargs="?", default=".", help="repository root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="show install commands without running them")
    args = parser.parse_args()

    root = Path(args.repository).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"repository path is not a directory: {root}")

    requirements = sorted(root.glob("requirements*.txt"))
    installs = detect_installs(root)
    if not requirements and not installs:
        print(f"[info] no supported dependency manifests found in {root}")
        return 0

    success = True
    for requirement_file in requirements:
        success = pip_install_requirements(requirement_file, root, args.dry_run) and success
    for label, command in installs:
        success = run_install(label, command, root, args.dry_run) and success

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
