# cerun GUI

Start it with `cerun-gui`, or from the application menu.

The first four rows select the executable to launch. Edit a path directly or
use Browse. Steam path is the installation folder containing `steamapps`.
Save paths writes `~/.config/cerun/executables.conf`; Launch also saves edits.
Blank numbered shortcuts are allowed; the default executable and Steam path are
required. An executable that is missing at the moment (for example on an
unmounted drive) is reported but only blocks launching that executable.
Reload paths reads external changes. A config file that cannot be parsed is
reported and can be replaced by saving the paths entered in the window.

Select a running .exe process in the table, then press Launch. The table is
refreshed whenever the window is activated and by Refresh processes; the
selection is retained only while the same process exists. Several executables
can run at once; Launch is disabled only for an executable that is already
running for the selected process.

The window calls its bundled plain-text
`cerun-backend.sh --pid PID /path/to/helper.exe /path/to/steam`, which runs
without a terminal and handles the Proton lookup, registry setup and Flatpak
runtime entry.

Log opens the latest launch log in the default application; after a failed
launch it opens the log of that launch. Logs are saved in
`~/.local/state/cerun` (the newest 20 are kept) and the latest is remembered
across restarts. Closing the window leaves launched applications running.

## Dependencies

Python 3 and PySide6 are required. The GUI runs on the host to see the process
list and access the existing Flatpak runtime.
