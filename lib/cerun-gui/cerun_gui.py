#!/usr/bin/python3
"""Native Qt frontend for cerun-configured. No shell evaluation or terminal."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QAbstractItemView,
)

KEYS = ("DEFAULT_EXE", "EXE_1", "EXE_2", "EXE_3", "STEAMPATH")
LABELS = ("Default", "EXE_1", "EXE_2", "EXE_3")
HOME_DIR = Path.home()
INSTALL_PREFIX = Path(__file__).resolve().parents[2]
ICON_PATH = INSTALL_PREFIX / "share/icons/hicolor/512x512/apps/cerun-gui.png"


def expand_path(value: str) -> str:
    if value.startswith("~/"):
        return str(HOME_DIR / value[2:])
    if value.startswith("$HOME/"):
        return str(HOME_DIR / value[6:])
    return value


def read_config(path: Path) -> dict[str, str]:
    values = dict.fromkeys(KEYS, "")
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in values:
            raise ValueError(f"Unrecognized config line: {line}")
        values[key] = expand_path(value)
    return values


def save_config(path: Path, values: dict[str, str]) -> None:
    for key in KEYS:
        value = values[key]
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: paths must fit on one line.")
        if not value and key in KEYS[1:4]:
            continue
        if not value or not Path(value).is_absolute():
            raise ValueError(f"{key}: enter an absolute path, without quotes.")
        if key == "STEAMPATH":
            if not (Path(value) / "steamapps").is_dir():
                raise ValueError("Steam path must be the folder containing steamapps.")
        elif not Path(value).is_file():
            raise ValueError(f"{key}: executable not found: {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".executables.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write("# Executable and Steam paths: plain values, no shell quotes.\n")
            for key in KEYS:
                out.write(f"{key}={values[key]}\n")
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    command: str
    started: str


def process_identity(pid: int, proc_root: Path = Path("/proc")) -> str:
    # comm can contain spaces and parentheses; stat field 22 is starttime.
    return (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]


def list_processes(proc_root: Path = Path("/proc")) -> list[Process]:
    result = []
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            pid = int(directory.name)
            started = process_identity(pid, proc_root)
            name = (directory / "comm").read_text().rstrip("\n")
            args = (directory / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            command = " ".join(os.fsdecode(arg) for arg in args)
            if name.lower().endswith(".exe") or ".exe" in command.lower():
                if started == process_identity(pid, proc_root):
                    result.append(Process(pid, name, command, started))
        except (OSError, ValueError, IndexError):
            continue  # A process can exit during refresh.
    return sorted(result, key=lambda item: item.pid)


class Window(QMainWindow):
    def __init__(self, config: Path | None = None, backend: Path | None = None,
                 state: Path | None = None, proc_root: Path = Path("/proc")):
        super().__init__()
        self.config = config or HOME_DIR / ".config/cerun/executables.conf"
        self.backend = backend or Path(__file__).resolve().with_name("cerun-backend.sh")
        self.state = state or HOME_DIR / ".local/state/cerun"
        self.proc_root = proc_root
        self.child: subprocess.Popen | None = None
        self.last_log: Path | None = None
        self.loaded_text: str | None = None
        self.setWindowTitle("cerun — Executable launcher")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1000, 700)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        helpers = QGroupBox("1. Choose an executable")
        helper_layout = QVBoxLayout(helpers)
        self.buttons = QButtonGroup(self)
        self.paths: dict[str, QLineEdit] = {}
        for index, key in enumerate(KEYS):
            row = QHBoxLayout()
            if index < 4:
                label = QRadioButton(LABELS[index])
                self.buttons.addButton(label, index)
                label.setChecked(index == 0)
            else:
                label = QLabel("Steam path")
            label.setMinimumWidth(105)
            row.addWidget(label)
            edit = QLineEdit()
            edit.setPlaceholderText("Folder containing steamapps" if index == 4 else "Executable path — no quotes")
            edit.textChanged.connect(self.update_launch)
            self.paths[key] = edit
            row.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda checked=False, k=key: self.browse(k))
            row.addWidget(browse)
            helper_layout.addLayout(row)
        config_row = QHBoxLayout()
        config_row.addWidget(QLabel("Paths with spaces are accepted. Blank shortcuts are optional."), 1)
        reload_button = QPushButton("Reload paths")
        reload_button.clicked.connect(self.load_paths)
        save_button = QPushButton("Save paths")
        save_button.clicked.connect(self.save_paths)
        config_row.addWidget(reload_button)
        config_row.addWidget(save_button)
        helper_layout.addLayout(config_row)
        layout.addWidget(helpers)

        layout.addWidget(QLabel("2. Choose the running process whose prefix to use"))
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
        layout.addWidget(self.table, 1)
        refresh_row = QHBoxLayout()
        self.count = QLabel()
        refresh_row.addWidget(self.count, 1)
        refresh_button = QPushButton("Refresh processes")
        refresh_button.clicked.connect(self.refresh)
        refresh_row.addWidget(refresh_button)
        layout.addLayout(refresh_row)

        self.status = QLabel("Choose an executable and a running process.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        self.log_button = QPushButton("Log")
        self.log_button.clicked.connect(self.open_log)
        self.launch_button = QPushButton("Launch")
        self.launch_button.setMinimumWidth(130)
        self.launch_button.clicked.connect(self.launch)
        actions.addWidget(self.log_button)
        actions.addWidget(self.launch_button)
        layout.addLayout(actions)
        self.buttons.idToggled.connect(self.update_launch)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        try:
            recent = Path((self.state / "last-log").read_text().strip())
            if recent.is_file(): self.last_log = recent
        except OSError:
            pass
        self.load_paths()
        self.refresh()

    def error(self, message: str) -> None:
        self.status.setText(message)
        QMessageBox.warning(self, "cerun", message)

    def load_paths(self) -> None:
        try:
            values = read_config(self.config)
            self.loaded_text = self.config.read_text() if self.config.exists() else None
            for key, value in values.items(): self.paths[key].setText(value)
            if not self.config.exists():
                self.status.setText("First setup: enter the default executable and Steam path, then save. Shortcuts are optional.")
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def save_paths(self) -> bool:
        try:
            current = self.config.read_text() if self.config.exists() else None
            if current != self.loaded_text:
                raise ValueError("Config changed outside this window. Reload paths before saving.")
            values = {key: expand_path(edit.text()) for key, edit in self.paths.items()}
            save_config(self.config, values)
            self.loaded_text = self.config.read_text()
            self.status.setText("Paths saved.")
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
        if chosen: self.paths[key].setText(chosen)

    def selected_process(self) -> Process | None:
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None

    def refresh(self) -> None:
        previous = self.selected_process()
        try:
            processes = list_processes(self.proc_root)
        except OSError as exc:
            self.error(f"Cannot read the process list: {exc}")
            return
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(processes))
        for row, process in enumerate(processes):
            for column, value in enumerate((str(process.pid), process.name, process.command)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0: item.setData(Qt.ItemDataRole.UserRole, process)
                self.table.setItem(row, column, item)
            if previous and (process.pid, process.started) == (previous.pid, previous.started):
                self.table.setCurrentCell(row, 0)
        self.table.blockSignals(False)
        self.count.setText(f"{len(processes)} executable processes found" if processes else "No .exe processes found. Start a game, then refresh.")
        self.update_launch()

    def update_launch(self, *unused) -> None:
        if not hasattr(self, "launch_button"): return
        index = self.buttons.checkedId()
        selected = index >= 0 and bool(self.paths[KEYS[index]].text())
        self.launch_button.setEnabled(bool(selected and self.selected_process() and self.child is None))
        self.log_button.setEnabled(self.last_log is not None and self.last_log.is_file())

    def launch(self) -> None:
        process = self.selected_process()
        index = self.buttons.checkedId()
        if process is None or index < 0 or self.child is not None: return
        try:
            if process_identity(process.pid, self.proc_root) != process.started:
                raise ValueError("Selected process has been replaced. Refresh and select it again.")
            if not self.backend.is_file() or not os.access(self.backend, os.X_OK):
                raise ValueError(f"Launcher not found or not executable: {self.backend}")
            if not self.save_paths(): return
            executable = expand_path(self.paths[KEYS[index]].text())
            self.state.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f"gui-{datetime.now():%Y%m%d-%H%M%S}-", suffix=".log", dir=self.state)
            self.last_log = Path(name)
            # A real file, not a pipe: closing this GUI does not kill the launcher
            # or break its output. All arguments remain separate, including spaces.
            with os.fdopen(fd, "wb") as output:
                self.child = subprocess.Popen([str(self.backend), "--pid", str(process.pid), executable],
                                              stdin=subprocess.DEVNULL, stdout=output,
                                              stderr=subprocess.STDOUT, start_new_session=True)
            (self.state / "last-log").write_text(str(self.last_log))
            self.status.setText(f"Launching {Path(executable).name} for {process.name} (PID {process.pid}).")
        except (OSError, ValueError, IndexError) as exc:
            self.error(f"Launch failed: {exc}")
        self.update_launch()

    def poll(self) -> None:
        if self.child is None: return
        status = self.child.poll()
        if status is not None:
            self.child = None
            self.status.setText("Launch command finished. See Log for details." if status == 0 else
                                f"Launch failed (exit {status}). Open Log for details.")
            self.update_launch()

    def open_log(self) -> None:
        if self.last_log is None or not self.last_log.is_file():
            self.error("No launch log is available yet.")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_log))):
            self.error(f"Could not open the log in your default application: {self.last_log}")


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("cerun")
    app.setDesktopFileName("cerun-gui")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = Window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
