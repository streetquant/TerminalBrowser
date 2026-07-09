from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .workspace_recipe import WorkspaceRecipe, RecipeTab, RecipeWindow, recipe_to_json

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:")


@dataclass(frozen=True)
class DetectionNote:
    kind: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class ProjectDetection:
    root: str
    name: str
    recipe: WorkspaceRecipe
    notes: tuple[DetectionNote, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "name": self.name,
            "notes": [note.to_dict() for note in self.notes],
            "recipe": self.recipe.to_dict(),
        }


def _project_name(root: Path) -> str:
    name = root.name.strip() or "workspace"
    return _SAFE.sub("-", name).strip("-") or "workspace"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _script_command(package_manager: str, script: str) -> str:
    if package_manager == "npm":
        return "npm test" if script == "test" else f"npm run {script}"
    if package_manager == "yarn":
        return f"yarn {script}"
    if package_manager == "pnpm":
        return f"pnpm {script}"
    if package_manager == "bun":
        return f"bun run {script}"
    return f"npm run {script}"


def _add_tab(tabs: list[RecipeTab], *, name: str, tag: str, command: str, cwd: Path, visible: bool = True) -> None:
    key = (name.casefold(), tag.casefold(), command.casefold(), str(cwd))
    existing = {(tab.name.casefold(), tab.tag.casefold(), tab.command.casefold(), tab.cwd) for tab in tabs}
    if key not in existing:
        tabs.append(RecipeTab(name=name, tag=tag, command=command, cwd=str(cwd), visible=visible))


def _detect_node(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote]) -> None:
    package_path = root / "package.json"
    data = _read_json(package_path)
    if not data:
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    package_manager = _package_manager(root)
    notes.append(DetectionNote("node", _rel(package_path, root), f"detected package.json; package manager: {package_manager}"))
    for candidate, label, tag in [
        ("dev", "dev server", "dev"),
        ("start", "app start", "dev"),
        ("serve", "serve", "dev"),
        ("test", "tests", "tests"),
        ("lint", "lint", "quality"),
    ]:
        if candidate in scripts:
            _add_tab(tabs, name=label, tag=tag, command=_script_command(package_manager, candidate), cwd=root)


def _detect_python(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote]) -> None:
    markers = ["pyproject.toml", "requirements.txt", "setup.py", "pytest.ini", "tox.ini"]
    found = [root / marker for marker in markers if (root / marker).exists()]
    tests_dir = root / "tests"
    if not found and not tests_dir.exists() and not (root / "manage.py").exists():
        return
    if found:
        notes.append(DetectionNote("python", _rel(found[0], root), "detected Python project marker"))
    if (root / "manage.py").exists():
        notes.append(DetectionNote("django", "manage.py", "detected Django-style manage.py"))
        _add_tab(tabs, name="django server", tag="dev", command="python3 manage.py runserver", cwd=root)
    if tests_dir.exists() or (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
        _add_tab(tabs, name="python tests", tag="tests", command="pytest -q", cwd=root)


def _make_targets(path: Path) -> set[str]:
    targets: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return targets
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("."):
            continue
        match = _TARGET.match(line)
        if match:
            target = match.group(1)
            if "%" not in target and target not in {"FORCE"}:
                targets.add(target)
    return targets


def _detect_makefile(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote]) -> None:
    for name in ("Makefile", "makefile"):
        path = root / name
        if not path.exists():
            continue
        targets = _make_targets(path)
        if not targets:
            return
        notes.append(DetectionNote("make", _rel(path, root), f"detected make targets: {', '.join(sorted(list(targets))[:8])}"))
        for candidate, label, tag in [("dev", "make dev", "dev"), ("run", "make run", "dev"), ("start", "make start", "dev"), ("test", "make test", "tests"), ("lint", "make lint", "quality")]:
            if candidate in targets:
                _add_tab(tabs, name=label, tag=tag, command=f"make {candidate}", cwd=root)
        return


def _detect_docker(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote]) -> None:
    compose_names = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    for name in compose_names:
        path = root / name
        if path.exists():
            notes.append(DetectionNote("docker", _rel(path, root), "detected Docker Compose file"))
            _add_tab(tabs, name="docker compose", tag="infra", command="docker compose up", cwd=root)
            return
    if (root / "Dockerfile").exists():
        notes.append(DetectionNote("docker", "Dockerfile", "detected Dockerfile"))
        image = _project_name(root).lower()
        _add_tab(tabs, name="docker build", tag="infra", command=f"docker build -t {image}:local .", cwd=root)


def _detect_logs(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote]) -> None:
    logs_dir = root / "logs"
    if not logs_dir.is_dir():
        return
    files = sorted([item for item in logs_dir.glob("*.log") if item.is_file()])[:3]
    if not files:
        return
    notes.append(DetectionNote("logs", "logs/", f"detected {len(files)} log file(s)"))
    patterns = " ".join(_rel(path, root) for path in files)
    _add_tab(tabs, name="logs", tag="logs", command=f"tail -F {patterns}", cwd=root)


def _detect_ai(root: Path, tabs: list[RecipeTab], notes: list[DetectionNote], *, include_ai: bool) -> None:
    if not include_ai:
        return
    notes.append(DetectionNote("ai", ".", "included optional AI-agent tabs"))
    _add_tab(tabs, name="codex", tag="ai", command="codex", cwd=root, visible=True)
    _add_tab(tabs, name="claude", tag="ai", command="claude", cwd=root, visible=False)


def detect_project(path: str | Path | None = None, *, include_ai: bool = False) -> ProjectDetection:
    root = Path(path or os.getcwd()).expanduser().resolve(strict=False)
    if root.is_file():
        root = root.parent
    name = _project_name(root)
    tabs: list[RecipeTab] = []
    notes: list[DetectionNote] = []

    _add_tab(tabs, name="shell", tag="default", command="{shell}", cwd=root)
    _detect_node(root, tabs, notes)
    _detect_python(root, tabs, notes)
    _detect_makefile(root, tabs, notes)
    _detect_docker(root, tabs, notes)
    _detect_logs(root, tabs, notes)
    _detect_ai(root, tabs, notes, include_ai=include_ai)

    if not notes:
        notes.append(DetectionNote("generic", ".", "no project markers found; created a shell-only workspace"))

    dev_tags = {"default", "dev", "tests", "quality", "logs", "infra"}
    dev_tabs = tuple(tab for tab in tabs if tab.tag in dev_tags)
    ai_tabs = tuple(tab for tab in tabs if tab.tag == "ai")
    windows: list[RecipeWindow] = [RecipeWindow(title="Development", tabs=dev_tabs or tuple(tabs))]
    if ai_tabs:
        windows.append(RecipeWindow(title="Agents", tabs=ai_tabs))
    recipe = WorkspaceRecipe(name=name, windows=tuple(windows), source_path=None)
    return ProjectDetection(root=str(root), name=name, recipe=recipe, notes=tuple(notes))


def project_detection_json(detection: ProjectDetection) -> str:
    return json.dumps(detection.to_dict(), indent=2, sort_keys=True)


def project_recipe_json(detection: ProjectDetection) -> str:
    return recipe_to_json(detection.recipe)
