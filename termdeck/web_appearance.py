"""Explicit, revision-checked appearance exchange with desktop defaults."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .settings import AppSettings, DEFAULT_CONFIG_PATH
from .ui_theme import CHROME_THEME_NAMES, TERMINAL_PALETTE_NAMES


def read_bytes(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b""
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > 1048576:
            raise ValueError("Desktop settings file is unsafe or too large")
        return stream.read(1048577)


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def exchange(request, path: Path | None = None):
    path = path or DEFAULT_CONFIG_PATH
    mode = request.get("mode", "read")
    if mode not in ("read", "save"):
        raise ValueError("Invalid appearance operation")
    raw = read_bytes(path)
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise ValueError("Desktop settings must be an object")
    if mode == "read":
        settings = AppSettings.load(path)
        font = settings.terminal_font or "Monospace 13"
        match = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?)", font)
        family, size = (match[1], round(float(match[2]))) if match else (font, 13)
        return {"ok": True, "revision": revision(raw), "values": {
            "chrome": settings.chrome_theme, "density": settings.ui_density,
            "palette": settings.terminal_palette, "fontSize": max(10, min(72, size)),
            "font": "system" if settings.terminal_use_system_font else "auto",
            "fontFamily": "" if settings.terminal_use_system_font else family,
            "cursorStyle": "bar" if settings.terminal_cursor_shape == "ibeam" else settings.terminal_cursor_shape or "block",
            "cursorColor": settings.terminal_cursor_color or "", "cursorBlink": settings.terminal_cursor_blink is not False,
            "scrollback": settings.terminal_scrollback_lines or 20000,
            "uiFont": re.sub(r"\s+\d+(?:\.\d+)?$", "", settings.ui_font or ""), "uiScale": settings.ui_font_scale or 1,
            "side": settings.side_pane_position, "tabsPane": settings.tabs_pane_visible, "reduceMotion": settings.reduce_motion,
        }}
    values = request.get("values")
    if not isinstance(values, dict) or request.get("confirmation") != "save-desktop-defaults":
        raise ValueError("Confirm saving desktop defaults")
    allowed = {"chrome", "density", "palette", "fontSize", "font", "fontFamily", "cursorStyle", "cursorColor", "cursorBlink", "scrollback", "uiFont", "uiScale", "side", "tabsPane", "reduceMotion"}
    if set(values) != allowed:
        raise ValueError("Incomplete appearance settings")
    for field, choices in {"chrome": CHROME_THEME_NAMES, "density": ("compact", "comfortable"), "palette": TERMINAL_PALETTE_NAMES, "font": ("auto", "system", "jetbrains", "cascadia", "iosevka", "liberation"), "cursorStyle": ("block", "bar", "underline"), "side": ("left", "right")}.items():
        if values[field] not in choices:
            raise ValueError(f"Invalid appearance {field}")
    for field, low, high in (("fontSize", 10, 72), ("scrollback", 100, 500000)):
        if type(values[field]) is not int or not low <= values[field] <= high:
            raise ValueError(f"Invalid appearance {field}")
    for field in ("cursorBlink", "tabsPane", "reduceMotion"):
        if type(values[field]) is not bool:
            raise ValueError(f"Invalid appearance {field}")
    if type(values["uiScale"]) not in (int, float) or not .75 <= values["uiScale"] <= 1.75:
        raise ValueError("Invalid interface scale")
    for field in ("uiFont", "fontFamily", "cursorColor"):
        value = values[field]
        if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
            raise ValueError(f"Invalid appearance {field}")
    if values["cursorColor"] and not re.fullmatch(r"#[0-9a-fA-F]{6}", values["cursorColor"]):
        raise ValueError("Invalid cursor color")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.geteuid():
        raise ValueError("Desktop settings directory is unsafe")
    lock_fd = os.open(path.parent / ".web-appearance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "rb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("Desktop settings lock is unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = read_bytes(path)
        if request.get("revision") != revision(raw):
            raise ValueError("Desktop settings changed. Load them again before saving.")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("Desktop settings must be an object")
        appearance = payload.setdefault("appearance", {})
        if not isinstance(appearance, dict):
            raise ValueError("Desktop appearance must be an object")
        font_families = {"auto": "Monospace", "system": "Monospace", "jetbrains": "JetBrains Mono", "cascadia": "Cascadia Mono", "iosevka": "Iosevka Term", "liberation": "Liberation Mono"}
        appearance.update(chrome_theme=values["chrome"], density=values["density"], terminal_palette=values["palette"],
                          ui_font=values["uiFont"], ui_font_scale=values["uiScale"], side_pane_position=values["side"],
                          tabs_pane_visible=values["tabsPane"], reduce_motion=values["reduceMotion"])
        terminal = appearance.setdefault("terminal", {})
        if not isinstance(terminal, dict):
            raise ValueError("Desktop terminal appearance must be an object")
        terminal.update(font=f'{values["fontFamily"] or font_families[values["font"]]} {values["fontSize"]}',
                        use_system_font=values["font"] == "system" and not values["fontFamily"],
                        cursor_shape="ibeam" if values["cursorStyle"] == "bar" else values["cursorStyle"],
                        cursor_blink=values["cursorBlink"], cursor_color=values["cursorColor"], scrollback_lines=values["scrollback"])
        terminal.pop("cursor_blinking", None)
        for key in ("focus_mode", "focus", "show_tabs_pane"):
            appearance.pop(key, None)
        encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        fd, filename = tempfile.mkstemp(prefix=".appearance-", dir=path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            AppSettings.load(temporary)  # Validate against the desktop contract.
            if read_bytes(path) != raw:
                raise ValueError("Desktop settings changed during save; nothing was overwritten")
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "revision": revision(encoded)}
