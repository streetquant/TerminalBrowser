# Contributing to TerminalBrowser

Thanks for your interest! TerminalBrowser welcomes bug reports, feature requests, and pull requests from everyone.

## Quick start

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/TerminalBrowser.git
cd TerminalBrowser

# 2. Install system dependencies (Debian/Ubuntu)
sudo apt install -y tmux python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91

# 3. Install in editable mode
pip install -e .

# 4. Create a branch
git checkout -b feat/my-feature

# 5. Make your changes, then run checks
python3 -m compileall -q termdeck tests
python3 -m pytest tests -q

# 6. Push and open a PR against main
git push origin feat/my-feature
```

## What to contribute

- 🐛 **Bug fixes** — open an issue first for anything non-trivial
- ✨ **Features** — open an issue to discuss before writing code
- 📝 **Docs** — typos, clarity, new examples, translations
- 🧪 **Tests** — more coverage is always welcome
- 🎨 **CSS / themes** — improvements to `termdeck/chrome_css.py`

## Code style

- Python 3.10+, type hints preferred
- No third-party runtime dependencies (GTK, VTE, and tmux are system packages)
- Keep GTK/UI logic in `gtk_app.py`; keep backend/session logic in `tmux_backend.py` and `session_store.py`
- All new CLI sub-commands go in `cli.py` and must work headlessly (no GTK import at the top level)

## Branch naming

| Type | Prefix | Example |
|---|---|---|
| Feature | `feat/` | `feat/broadcast-send` |
| Bug fix | `fix/` | `fix/tab-focus-sidepane` |
| Docs | `docs/` | `docs/arch-install` |
| Refactor | `refactor/` | `refactor/resources-module` |

## PR checklist

Before opening a PR:
- [ ] `python3 -m compileall -q termdeck tests` passes
- [ ] `python3 -m pytest tests -q` passes
- [ ] New behaviour has a test or a clear reason why one isn’t needed
- [ ] No new third-party Python runtime dependencies added
- [ ] Commit messages are imperative present tense (`fix: ...`, `feat: ...`, `docs: ...`)

## Reporting bugs

Open a [GitHub Issue](https://github.com/streetquant/TerminalBrowser/issues) with:
- OS and distro version
- Python version (`python3 --version`)
- GTK/VTE version (`pkg-config --modversion vte-2.91`)
- Steps to reproduce
- What you expected vs. what happened

## License

By contributing you agree your changes are licensed under the project’s [MIT License](LICENSE).
