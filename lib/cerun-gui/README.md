# cerun GUI

Run `./cerun-gui` from the enclosing package directory.

The first four rows select the executable to launch. Edit a path directly or
use Browse. Steam path is the installation folder containing `steamapps`.
Save paths writes the existing `~/.config/cerun/executables.conf` format.
Launch also saves edits. Blank numbered shortcuts are allowed; the default
executable and Steam path are required. Reload paths reads external changes.

Select a running .exe process in the table, then press Launch. Refresh processes
updates the table and retains the selection only while the same process exists.
The wrapper calls its bundled plain-text `cerun-backend.sh --pid PID /path/to/helper.exe`, which skips
all terminal windows and interactive prompts. The backend retains its Proton
lookup, registry setup, and Flatpak runtime entry behavior.

Log opens the latest GUI launch log in the default application, including failed
launch output. Logs are saved in `~/.local/state/cerun`; the latest is remembered
across GUI restarts. The backend also retains its normal `/tmp/cerun.*.log`.
Closing the GUI leaves launched applications running.

## Dependencies and development

Python 3 and PySide6 are already installed on this system. No container is
required or used: the GUI runs on the host to see the actual process list and
access the existing Flatpak runtime. Source and tests live in this directory. The Bash backend is bundled here as plain
text; the GUI does not invoke or modify `~/.local/bin/cerun-configured`.

Run checks with `QT_QPA_PLATFORM=offscreen python3 -m unittest -v test_gui.py`
from this directory. Tests use temporary config files and mock launchers; they
do not start games or edit the real config.
