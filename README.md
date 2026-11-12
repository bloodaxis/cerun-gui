# Cerun GUI

A Qt frontend for launching executables inside a running proton/wine prefix.

## Install

Download `cerun-gui-local-v1.0.6.tar.gz` from the latest release and extract it
into `~/.local`:

```bash
tar -xzf cerun-gui-local-v1.0.6.tar.gz -C ~/.local
```

Requires `/usr/bin/python3` with PySide6 installed. The launcher, desktop entry,
and user-theme icon are installed under the standard `~/.local` directories.

## Development

```bash
QT_QPA_PLATFORM=offscreen python3 -B -m unittest -v tests/test_gui.py
./package.sh    # writes ../cerun-gui-local-v<version>.tar.gz
```

The tests use temporary config files and mock launchers; they do not start
games or touch the real config. `tests/` and `package.sh` are not part of the
release archive.
