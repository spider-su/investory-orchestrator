from __future__ import annotations

import os
from pathlib import Path


IGNORED_DIRECTORIES = {
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    "target",
    "build",
    "dist",
    ".gradle",
}

CONTEXT_FILES = (
    "AGENTS.md",
    "README.md",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "mvnw",
)


def _read_text(path: Path, *, limit: int = 12_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"<unable to read {path.name}: {error}>"

    if len(text) <= limit:
        return text

    return text[:limit] + "\n... <truncated>"


def _top_level_tree(
    workspace: Path,
    *,
    max_depth: int = 3,
    max_entries: int = 250,
) -> str:
    entries: list[str] = []

    for root, directories, files in os.walk(workspace):
        root_path = Path(root)
        relative_root = root_path.relative_to(workspace)
        depth = len(relative_root.parts)

        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_DIRECTORIES
        )

        if depth >= max_depth:
            directories[:] = []

        for directory in directories:
            entries.append(
                f"{(relative_root / directory).as_posix()}/"
            )

        for file_name in sorted(files):
            entries.append(
                (relative_root / file_name).as_posix()
            )

        if len(entries) >= max_entries:
            entries = entries[:max_entries]
            entries.append("... <tree truncated>")
            break

    return "\n".join(entries)


def _java_packages(
    workspace: Path,
    *,
    limit: int = 40,
) -> list[str]:
    packages: set[str] = set()

    for source_root in (
        workspace / "src/main/java",
        workspace / "src/test/java",
    ):
        if not source_root.exists():
            continue

        for java_file in source_root.rglob("*.java"):
            try:
                lines = java_file.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()[:30]
            except OSError:
                continue

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("package ") and stripped.endswith(";"):
                    packages.add(
                        stripped.removeprefix("package ")
                        .removesuffix(";")
                        .strip()
                    )
                    break

            if len(packages) >= limit:
                break

    return sorted(packages)


def _representative_tests(
    workspace: Path,
    *,
    count: int = 3,
) -> list[Path]:
    candidates: list[Path] = []

    for test_root in (
        workspace / "src/test/java",
        workspace / "src/test/kotlin",
        workspace / "tests",
        workspace / "test",
    ):
        if not test_root.exists():
            continue

        candidates.extend(
            path
            for path in test_root.rglob("*")
            if path.is_file()
            and (
                path.name.endswith("Test.java")
                or path.name.endswith("Tests.java")
                or path.name.endswith("Test.kt")
                or path.name.startswith("test_")
            )
        )

    return sorted(candidates)[:count]

