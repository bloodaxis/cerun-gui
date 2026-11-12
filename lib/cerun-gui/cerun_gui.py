#!/usr/bin/python3
"""Native Qt frontend for the bundled Proton launcher."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QEvent, QRegularExpression, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QAbstractItemView,
)

VERSION = "1.0.6"
KEYS = ("DEFAULT_EXE", "EXE_1", "EXE_2", "EXE_3", "STEAMPATH")
EXE_KEYS = KEYS[:4]
OPTIONAL_KEYS = KEYS[1:4]
LABELS = ("Default", "EXE_1", "EXE_2", "EXE_3")
CONFIG_HEADER = "# Executable and Steam paths: plain values, no shell quotes.\n"
BACKEND_NAME = "cerun-backend.sh"
KEEP_LOGS = 20
COMM_LIMIT = 15  # The kernel truncates /proc/PID/comm to this many characters.
HOME_DIR = Path.home()
INSTALL_PREFIX = Path(__file__).resolve().parents[2]
ICON_PATH = INSTALL_PREFIX / "share/icons/hicolor/512x512/apps/cerun-gui.png"


def expand_path(value: str) -> str:
    value = value.strip()
    if value.startswith("~/"):
        return str(HOME_DIR / value[2:])
    if value.startswith("$HOME/"):
        return str(HOME_DIR / value[6:])
    return value


def parse_config(text: str) -> dict[str, str]:
    values = dict.fromkeys(KEYS, "")
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in values:
            raise ValueError(f"Unrecognized config line: {line}")
        values[key] = expand_path(value)
    return values


def read_config(path: Path) -> dict[str, str]:
    return parse_config(path.read_text()) if path.exists() else dict.fromkeys(KEYS, "")


def render_config(values: dict[str, str]) -> str:
    return CONFIG_HEADER + "".join(f"{key}={values[key]}\n" for key in KEYS)


def validate_config(values: dict[str, str]) -> None:
    """Reject values the launcher cannot use. A missing executable is not an
    error here: a shortcut on an unmounted drive must not block the others."""
    for key in KEYS:
        value = values[key]
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: paths must fit on one line.")
        if not value and key in OPTIONAL_KEYS:
            continue
        if not value or not Path(value).is_absolute():
            raise ValueError(f"{key}: enter an absolute path, without quotes.")
    if not (Path(values["STEAMPATH"]) / "steamapps").is_dir():
        raise ValueError("Steam path must be the folder containing steamapps.")


def missing_executables(values: dict[str, str]) -> list[str]:
    return [key for key in EXE_KEYS if values[key] and not Path(values[key]).is_file()]


def save_config(path: Path, values: dict[str, str]) -> None:
    validate_config(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".executables.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(render_config(values))
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    command: str
    started: str


@dataclass
class Launch:
    child: subprocess.Popen
    label: str
    log: Path
    key: tuple[str, int, str]  # executable, PID, process start time


def process_identity(pid: int, proc_root: Path = Path("/proc")) -> str:
    # comm can contain spaces and parentheses; stat field 22 is starttime.
    return (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]


def display_name(comm: str, args: list[str]) -> str:
    """Complete a truncated comm from the first argument, when they agree."""
    if len(comm) < COMM_LIMIT or not args:
        return comm
    base = re.split(r"[\\/]", args[0])[-1]
    end = base.lower().find(".exe")
    if end >= 0:
        base = base[:end + 4]  # Wine can store the whole command line in argv[0].
    return base if base.startswith(comm) else comm


def list_processes(proc_root: Path = Path("/proc")) -> list[Process]:
    result = []
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            pid = int(directory.name)
            started = process_identity(pid, proc_root)
            comm = (directory / "comm").read_text().rstrip("\n")
            raw = (directory / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            args = [os.fsdecode(arg) for arg in raw]
            command = " ".join(args)
            if not comm.lower().endswith(".exe") and ".exe" not in command.lower():
                continue
            if any(arg.endswith("/" + BACKEND_NAME) for arg in args[:2]):
                continue  # This application's own launcher is never a usable target.
            if started == process_identity(pid, proc_root):
                result.append(Process(pid, display_name(comm, args), command, started))
        except (OSError, ValueError, IndexError):
            continue  # A process can exit during refresh.
    return sorted(result, key=lambda item: item.pid)


def prune_logs(directory: Path, keep: int = KEEP_LOGS) -> None:
    # The timestamp prefix makes name order chronological.
    for old in sorted(directory.glob("gui-*.log"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


class SingleLinePathEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"[^\r\n]*"), self))

    def setText(self, value: str) -> None:
        if "\n" not in value and "\r" not in value:
            super().setText(value)


class Window(QMainWindow):
    def __init__(self, config: Path | None = None, backend: Path | None = None,
                 state: Path | None = None, proc_root: Path = Path("/proc")):
        super().__init__()
        self.config = config or HOME_DIR / ".config/cerun/executables.conf"
        self.backend = backend or Path(__file__).resolve().with_name(BACKEND_NAME)
        self.state = state or HOME_DIR / ".local/state/cerun"
        self.proc_root = proc_root
        self.launches: list[Launch] = []
        self.last_log: Path | None = None
        self.loaded_text: str | None = None
        self.setWindowTitle("cerun — Executable launcher")
        self.setWindowIcon(app_icon())
        self.resize(1000, 700)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(self._build_paths())
        layout.addWidget(QLabel("2. Choose the running process whose prefix to use"))
        layout.addWidget(self._build_table(), 1)
        layout.addLayout(self._build_refresh_row())
        self.status = QLabel("Choose an executable and a running process.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addLayout(self._build_actions())

        self.timer = QTimer(self)  # Runs only while a launch is being watched.
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.poll)
        try:
            recent = Path((self.state / "last-log").read_text().strip())
            if recent.is_file():
                self.last_log = recent
        except OSError:
            pass
        self.load_paths()
        self.refresh()

    # ---- construction -------------------------------------------------

    def _build_paths(self) -> QGroupBox:
        group = QGroupBox("1. Choose an executable")
        layout = QVBoxLayout(group)
        self.buttons = QButtonGroup(self)
        self.paths: dict[str, QLineEdit] = {}
        for index, key in enumerate(KEYS):
            row = QHBoxLayout()
            if key in EXE_KEYS:
                label = QRadioButton(LABELS[index])
                self.buttons.addButton(label, index)
                label.setChecked(index == 0)
            else:
                label = QLabel("Steam path")
            label.setMinimumWidth(105)
            row.addWidget(label)
            edit = SingleLinePathEdit()
            edit.setPlaceholderText("Executable path" if key in EXE_KEYS else "Folder containing steamapps")
            edit.textChanged.connect(self.update_launch)
            self.paths[key] = edit
            row.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda checked=False, k=key: self.browse(k))
            row.addWidget(browse)
            layout.addLayout(row)
        self.buttons.idToggled.connect(self.update_launch)
        row = QHBoxLayout()
        row.addWidget(QLabel("Paths with spaces are accepted. Blank shortcuts are optional."), 1)
        for text, slot in (("Reload paths", self.load_paths), ("Save paths", self.save_paths)):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, s=slot: s())
            row.addWidget(button)
        layout.addLayout(row)
        return group

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["PID", "Process", "Launch command"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 85)
        self.table.setColumnWidth(1, 190)
        self.table.itemSelectionChanged.connect(self.update_launch)
        return self.table

    def _build_refresh_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.count = QLabel()
        row.addWidget(self.count, 1)
        button = QPushButton("Refresh processes")
        button.clicked.connect(lambda checked=False: self.refresh())
        row.addWidget(button)
        return row

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch()
        self.log_button = QPushButton("Log")
        self.log_button.clicked.connect(self.open_log)
        self.launch_button = QPushButton("Launch")
        self.launch_button.setMinimumWidth(130)
        self.launch_button.clicked.connect(self.launch)
        row.addWidget(self.log_button)
        row.addWidget(self.launch_button)
        return row

    # ---- paths --------------------------------------------------------

    def error(self, message: str) -> None:
        self.status.setText(message)
        QMessageBox.warning(self, "cerun", message)

    def values(self) -> dict[str, str]:
        return {key: expand_path(edit.text()) for key, edit in self.paths.items()}

    def disk_text(self) -> str | None:
        return self.config.read_text() if self.config.exists() else None

    def load_paths(self) -> None:
        try:
            text = self.disk_text()
        except (OSError, ValueError) as exc:
            self.error(f"Cannot read {self.config}: {exc}")
            return
        # Remember what was read even when it does not parse, so that the
        # paths entered here can replace a damaged file.
        self.loaded_text = text
        if text is None:
            self.status.setText("First setup: enter the default executable and Steam path, then save. Shortcuts are optional.")
            return
        try:
            values = parse_config(text)
        except ValueError as exc:
            self.error(f"{exc}\nCorrect {self.config}, or enter the paths here and save to replace it.")
            return
        for key, value in values.items():
            self.paths[key].setText(value)

    def save_paths(self, launching: str | None = None) -> bool:
        """Save the fields. `launching` names the key whose executable must exist."""
        try:
            current = self.disk_text()
            if current != self.loaded_text:
                raise ValueError("Config changed outside this window. Reload paths before saving.")
            values = self.values()
            validate_config(values)
            missing = missing_executables(values)
            if launching in missing:
                raise ValueError(f"{launching}: executable not found: {values[launching]}")
            if render_config(values) != current:
                save_config(self.config, values)
                self.loaded_text = self.disk_text()
            if launching is None:
                self.status.setText("Paths saved." + (f" Not found at the moment: {', '.join(missing)}." if missing else ""))
            return True
        except (OSError, ValueError) as exc:
            self.error(str(exc))
            return False

    def browse(self, key: str) -> None:
        current = expand_path(self.paths[key].text())
        if key == "STEAMPATH":
            chosen = QFileDialog.getExistingDirectory(self, "Choose Steam installation", current)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose executable", current,
                                                    "Windows executables (*.exe *.EXE);;All files (*)")
        if chosen:
            self.paths[key].setText(chosen)

    # ---- processes ----------------------------------------------------

    def selected_process(self) -> Process | None:
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None

    def refresh(self, quiet: bool = False) -> None:
        previous = self.selected_process()
        try:
            processes = list_processes(self.proc_root)
        except OSError as exc:
            message = f"Cannot read the process list: {exc}"
            # Closing a message box reactivates the window; never answer that with another.
            self.status.setText(message) if quiet else self.error(message)
            return
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(processes))
        for row, process in enumerate(processes):
            for column, value in enumerate((str(process.pid), process.name, process.command)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, process)
                self.table.setItem(row, column, item)
            if previous and (process.pid, process.started) == (previous.pid, previous.started):
                self.table.setCurrentCell(row, 0)
        self.table.blockSignals(False)
        self.count.setText(f"{len(processes)} executable processes found" if processes else "No .exe processes found. Start a game, then refresh.")
        self.update_launch()

    def changeEvent(self, event) -> None:
        # Coming back from starting a game: show it without a manual refresh.
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and hasattr(self, "timer"):
            self.refresh(quiet=True)
        super().changeEvent(event)

    # ---- launching ----------------------------------------------------

    def launch_key(self) -> tuple[str, int, str] | None:
        process = self.selected_process()
        index = self.buttons.checkedId()
        executable = expand_path(self.paths[KEYS[index]].text()) if index >= 0 else ""
        return (executable, process.pid, process.started) if process and executable else None

    def update_launch(self, *unused) -> None:
        key = self.launch_key()
        running = any(launch.key == key for launch in self.launches)
        self.launch_button.setEnabled(key is not None and not running)
        self.launch_button.setToolTip("This executable is already running for the selected process." if running else "")
        self.log_button.setEnabled(self.last_log is not None and self.last_log.is_file())

    def launch(self) -> None:
        process = self.selected_process()
        key = self.launch_key()
        if process is None or key is None or any(launch.key == key for launch in self.launches):
            return
        try:
            if process_identity(process.pid, self.proc_root) != process.started:
                raise ValueError("Selected process has been replaced. Refresh and select it again.")
            if not self.backend.is_file() or not os.access(self.backend, os.X_OK):
                raise ValueError(f"Launcher not found or not executable: {self.backend}")
            if not self.save_paths(launching=KEYS[self.buttons.checkedId()]):
                return
            executable = key[0]
            steam_path = expand_path(self.paths["STEAMPATH"].text())
            self.state.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f"gui-{datetime.now():%Y%m%d-%H%M%S}-", suffix=".log", dir=self.state)
            log = Path(name)
            # A real file, not a pipe: closing this GUI does not kill the launcher
            # or break its output. All arguments remain separate, including spaces.
            with os.fdopen(fd, "wb") as output:
                child = subprocess.Popen([str(self.backend), "--pid", str(process.pid), executable, steam_path],
                                         stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            label = f"{Path(executable).name} for {process.name} (PID {process.pid})"
            self.launches.append(Launch(child, label, log, key))
            self.timer.start()
            self.last_log = log
            (self.state / "last-log").write_text(str(log))
            prune_logs(self.state)
            self.status.setText(f"Launched {label}.")
        except (OSError, ValueError, IndexError) as exc:
            self.error(f"Launch failed: {exc}")
        self.update_launch()

    def poll(self) -> None:
        for launch in self.launches[:]:
            status = launch.child.poll()
            if status is None:
                continue
            self.launches.remove(launch)
            if status == 0:
                self.status.setText(f"{launch.label} has exited.")
            else:
                # Point the Log button at the launch that needs attention.
                if launch.log.is_file():
                    self.last_log = launch.log
                self.status.setText(f"{launch.label} failed (exit {status}). Open Log for details.")
        if not self.launches:
            self.timer.stop()
        self.update_launch()

    def open_log(self) -> None:
        if self.last_log is None or not self.last_log.is_file():
            self.error("No launch log is available yet.")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_log))):
            self.error(f"Could not open the log in your default application: {self.last_log}")


def app_icon() -> QIcon:
    return QIcon(str(ICON_PATH)) if ICON_PATH.is_file() else QIcon.fromTheme("cerun-gui")


def main() -> None:
    if "--version" in sys.argv[1:]:
        print(f"cerun-gui {VERSION}")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("cerun")
    app.setApplicationVersion(VERSION)
    app.setDesktopFileName("cerun-gui")
    app.setWindowIcon(app_icon())
    window = Window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
