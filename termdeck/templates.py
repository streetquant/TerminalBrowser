from __future__ import annotations

import json
import os
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .task_templates import BUILTIN_TEMPLATES, render_command


@dataclass(frozen=True)
class TaskTemplate:
    """A lightweight launch preset shown in the browser-like new-tab palette.

    This module keeps a stable public API while ``task_templates`` carries the
    dependency-availability metadata used by the GUI and CLI.
    """

    id: str
    label: str
    command: str
    name: str
    tag: str
    description: str

    @property
    def key(self) -> str:
        return self.id

    def materialise(self, cwd: str | None = None) -> tuple[str, str, str, str]:
        path = str(Path(cwd or os.getcwd()).expanduser())
        return self.name, self.tag, render_command(self.command, cwd=path), path

    def with_cwd(self, cwd: str | None = None) -> dict[str, str]:
        name, tag, command, path = self.materialise(cwd)
        return {
            "id": self.id,
            "key": self.id,
            "label": self.label,
            "name": name,
            "tag": tag,
            "command": command,
            "cwd": path,
            "description": self.description,
        }


def _from_task_template(template) -> TaskTemplate:
    return TaskTemplate(
        id=template.id,
        label=template.label,
        command=template.command,
        name=template.name,
        tag=template.tag,
        description=template.description,
    )


def builtin_templates() -> tuple[TaskTemplate, ...]:
    return tuple(_from_task_template(template) for template in BUILTIN_TEMPLATES)


def search_templates(query: str, templates: Iterable[TaskTemplate] | None = None) -> list[TaskTemplate]:
    query = query.casefold().strip()
    items = list(templates if templates is not None else builtin_templates())
    if not query:
        return items
    terms = [term for term in query.split() if term]
    results: list[TaskTemplate] = []
    for template in items:
        haystack = " ".join(
            [template.id, template.label, template.command, template.name, template.tag, template.description]
        ).casefold()
        if all(term in haystack for term in terms):
            results.append(template)
    return results


def get_template(key: str, templates: Iterable[TaskTemplate] | None = None) -> TaskTemplate | None:
    wanted = key.casefold().strip()
    for template in templates if templates is not None else builtin_templates():
        if template.id.casefold() == wanted:
            return template
    return None


def template_by_key(key: str, templates: Iterable[TaskTemplate] | None = None) -> TaskTemplate | None:
    return get_template(key, templates)


def split_command(command: str) -> list[str]:
    return shlex.split(command)


def templates_json() -> str:
    return json.dumps([asdict(template) for template in builtin_templates()], indent=2, sort_keys=True)
