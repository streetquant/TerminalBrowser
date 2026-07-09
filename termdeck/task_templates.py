from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class TaskTemplate:
    """A browser-like new-tab template for one terminal task."""

    id: str
    label: str
    name: str
    tag: str
    command: str
    description: str
    requires: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Compatibility alias used by early prototypes."""
        return self.id

    def materialise(self, cwd: str | Path | None = None, **values: str) -> tuple[str, str, str, str]:
        """Return name, tag, rendered command and working directory."""
        working_dir = str(Path(cwd or os.getcwd()).expanduser())
        rendered = render_command(self.command, shell=values.get("shell"), cwd=working_dir)
        return self.name, self.tag, rendered, working_dir

    def with_cwd(self, cwd: str | Path | None = None) -> dict[str, str]:
        name, tag, command, working_dir = self.materialise(cwd)
        return {
            "id": self.id,
            "key": self.id,
            "label": self.label,
            "name": name,
            "tag": tag,
            "command": command,
            "cwd": working_dir,
            "description": self.description,
        }


@dataclass(frozen=True)
class TemplateStatus:
    template: TaskTemplate
    available: bool
    missing: tuple[str, ...] = ()


BUILTIN_TEMPLATES: tuple[TaskTemplate, ...] = (
    TaskTemplate(
        id="shell",
        label="Local shell",
        name="shell",
        tag="default",
        command="{shell}",
        description="Open your normal interactive shell.",
    ),
    TaskTemplate(
        id="codex",
        label="Codex agent",
        name="codex",
        tag="ai",
        command="codex",
        description="Start an OpenAI Codex terminal-agent session.",
        requires=("codex",),
    ),
    TaskTemplate(
        id="claude",
        label="Claude Code",
        name="claude-code",
        tag="ai",
        command="claude",
        description="Start an Anthropic Claude Code session if installed.",
        requires=("claude",),
    ),
    TaskTemplate(
        id="opencode",
        label="OpenCode",
        name="opencode",
        tag="ai",
        command="opencode",
        description="Start an OpenCode terminal-agent session if installed.",
        requires=("opencode",),
    ),
    TaskTemplate(
        id="npm-dev",
        label="npm dev server",
        name="npm-dev",
        tag="dev",
        command="npm run dev",
        description="Run the common JavaScript development server command.",
        requires=("npm",),
    ),
    TaskTemplate(
        id="pytest-watch",
        label="pytest watcher",
        name="pytest",
        tag="tests",
        command="pytest -f",
        description="Run pytest in fail-fast/watch-style mode where supported.",
        requires=("pytest",),
    ),
    TaskTemplate(
        id="python-repl",
        label="Python REPL",
        name="python",
        tag="repl",
        command="python3",
        description="Open a Python interpreter.",
        requires=("python3",),
    ),
    TaskTemplate(
        id="node-repl",
        label="Node REPL",
        name="node",
        tag="repl",
        command="node",
        description="Open a Node.js interpreter.",
        requires=("node",),
    ),
    TaskTemplate(
        id="ssh",
        label="SSH session",
        name="ssh",
        tag="ssh",
        command="ssh user@host",
        description="Start from an editable SSH command template.",
        requires=("ssh",),
    ),
    TaskTemplate(
        id="docker-shell",
        label="Docker shell",
        name="docker-shell",
        tag="docker",
        command="docker exec -it container_name /bin/sh",
        description="Start from an editable docker exec shell template.",
        requires=("docker",),
    ),
    TaskTemplate(
        id="tail-log",
        label="Tail log file",
        name="logs",
        tag="logs",
        command="tail -f app.log",
        description="Follow an editable log file path.",
        requires=("tail",),
    ),
    TaskTemplate(
        id="custom",
        label="Custom command",
        name="task",
        tag="default",
        command="",
        description="Type any command manually.",
    ),
)


def default_shell() -> str:
    return os.environ.get("SHELL", "/bin/sh")


def render_command(command: str, *, shell: str | None = None, cwd: str | Path | None = None) -> str:
    """Render placeholders used by built-in task templates."""
    rendered = command.replace("{shell}", shell or default_shell())
    if cwd is not None:
        rendered = rendered.replace("{cwd}", shlex.quote(str(Path(cwd).expanduser())))
    return rendered


def builtin_templates() -> tuple[TaskTemplate, ...]:
    """Return built-in new-tab templates in display order."""
    return BUILTIN_TEMPLATES


def template_by_id(template_id: str) -> TaskTemplate:
    wanted = template_id.casefold().strip()
    for template in BUILTIN_TEMPLATES:
        if template.id.casefold() == wanted:
            return template
    raise KeyError(template_id)


def template_by_key(template_id: str) -> TaskTemplate | None:
    try:
        return template_by_id(template_id)
    except KeyError:
        return None


def get_template(template_id: str) -> TaskTemplate | None:
    return template_by_key(template_id)


def search_templates(query: str, templates: Iterable[TaskTemplate] | None = None) -> list[TaskTemplate]:
    """Search templates by id, label, command, tag, name or description."""
    items = list(templates if templates is not None else BUILTIN_TEMPLATES)
    query = query.casefold().strip()
    if not query:
        return items
    terms = [term for term in query.split() if term]
    results: list[tuple[int, TaskTemplate]] = []
    for template in items:
        haystack = " ".join(
            [template.id, template.label, template.name, template.tag, template.command, template.description]
        ).casefold()
        if all(term in haystack for term in terms):
            score = 0
            if template.id.casefold().startswith(query):
                score += 4
            if template.label.casefold().startswith(query):
                score += 3
            if query in template.command.casefold():
                score += 2
            results.append((score, template))
    results.sort(key=lambda item: (-item[0], item[1].label.casefold()))
    return [template for _score, template in results]


def template_statuses(which: Callable[[str], str | None] | None = None) -> list[TemplateStatus]:
    """Return built-in templates with dependency availability."""
    resolver = which or shutil.which
    statuses: list[TemplateStatus] = []
    for template in BUILTIN_TEMPLATES:
        missing = tuple(binary for binary in template.requires if resolver(binary) is None)
        statuses.append(TemplateStatus(template=template, available=not missing, missing=missing))
    return statuses


def search_template_statuses(
    query: str, which: Callable[[str], str | None] | None = None
) -> list[TemplateStatus]:
    found_ids = {template.id for template in search_templates(query)}
    return [status for status in template_statuses(which=which) if status.template.id in found_ids]


def serialise_statuses(statuses: Iterable[TemplateStatus]) -> list[dict[str, object]]:
    return [
        {
            "id": status.template.id,
            "label": status.template.label,
            "name": status.template.name,
            "tag": status.template.tag,
            "command": render_command(status.template.command),
            "description": status.template.description,
            "requires": list(status.template.requires),
            "available": status.available,
            "missing": list(status.missing),
        }
        for status in statuses
    ]


def split_command(command: str) -> list[str]:
    return shlex.split(command)
