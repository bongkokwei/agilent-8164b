"""Main window for the 8164B laser GUI."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QMetaObject, QObject, QSettings, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .widgets import ConnectionPanel, LogPanel, OutputPanel, ReadoutPanel, SweepPanel
from .worker import LaserWorker

logger = logging.getLogger(__name__)


class _LogBridge(QObject):
    """Carries log records from any thread onto the GUI thread."""

    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """Logging handler that forwards formatted records to a Qt signal."""

    def __init__(self):
        super().__init__()
        self.bridge = _LogBridge()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.message.emit(self.format(record))
        except RuntimeError:
            pass  # the window went away while a record was in flight


class LaserMainWindow(QMainWindow):
    """Ties the control panels to a :class:`LaserWorker` on its own thread."""

    # Requests that need arguments are emitted rather than called: a direct
    # call would execute the driver in the GUI thread and block the interface.
    laser_on_requested = pyqtSignal(bool)
    sweep_start_requested = pyqtSignal(object)
    sweep_configure_requested = pyqtSignal(object)

    def __init__(self, simulate: bool = False, poll_interval_ms: int = 200):
        super().__init__()
        self._simulate = simulate
        self._connected = False
        self._confirm_output = True

        self.setWindowTitle(
            "Agilent 8164B laser control" + (" — SIMULATION" if simulate else "")
        )
        self.resize(1180, 760)

        self._build_ui()
        self._build_menus()
        self._start_worker(simulate, poll_interval_ms)
        self._connect_signals()
        self._install_log_handler()
        self._restore_settings()

        self.worker_list_resources()
        self._set_controls_enabled(False)

    # -- construction --------------------------------------------------
    def _build_ui(self) -> None:
        self.connection_panel = ConnectionPanel()
        self.readout_panel = ReadoutPanel()
        self.output_panel = OutputPanel()
        self.sweep_panel = SweepPanel()
        self.log_panel = LogPanel()

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.connection_panel)
        left_layout.addWidget(self.readout_panel)
        left_layout.addWidget(self.output_panel)
        left_layout.addWidget(self.sweep_panel)
        left_layout.addStretch(1)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log_panel)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_column)
        splitter.addWidget(log_box)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 760])
        self.setCentralWidget(splitter)

        self.status_label = QLabel("Not connected")
        self.statusBar().addWidget(self.status_label, 1)
        if self._simulate:
            badge = QLabel("SIMULATION — no hardware")
            badge.setStyleSheet(
                "background-color: #ef6c00; color: white; padding: 1px 6px;"
                " border-radius: 3px;"
            )
            self.statusBar().addPermanentWidget(badge)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        instrument_menu = self.menuBar().addMenu("&Instrument")
        self.laser_off_action = QAction("Output &off now", self)
        self.laser_off_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.laser_off_action.setToolTip("Immediately stop any sweep and disable the output")
        self.laser_off_action.triggered.connect(self._emergency_off)
        instrument_menu.addAction(self.laser_off_action)
        instrument_menu.addSeparator()

        self.reset_action = QAction("&Reset (*RST)", self)
        self.reset_action.triggered.connect(self._reset_instrument)
        instrument_menu.addAction(self.reset_action)

        self.errors_action = QAction("Read &error queue", self)
        self.errors_action.triggered.connect(lambda: self._invoke_worker("flush_errors"))
        instrument_menu.addAction(self.errors_action)

        instrument_menu.addSeparator()
        self.confirm_action = QAction("Confirm before enabling output", self)
        self.confirm_action.setCheckable(True)
        self.confirm_action.setChecked(True)
        self.confirm_action.toggled.connect(self._set_confirm_output)
        instrument_menu.addAction(self.confirm_action)

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _start_worker(self, simulate: bool, poll_interval_ms: int) -> None:
        self.thread = QThread(self)
        self.worker = LaserWorker(simulate=simulate, poll_interval_ms=poll_interval_ms)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.thread.start()

    def _connect_signals(self) -> None:
        # GUI -> worker. Qt marshals these across the thread boundary.
        self.connection_panel.connect_requested.connect(self.worker.connect_to)
        self.connection_panel.disconnect_requested.connect(self.worker.disconnect_from)
        self.connection_panel.refresh_requested.connect(self.worker_list_resources)

        self.output_panel.laser_toggled.connect(self._on_laser_toggled)
        self.output_panel.wavelength_requested.connect(self.worker.set_wavelength_nm)
        self.output_panel.power_requested.connect(self.worker.set_power)
        self.output_panel.power_unit_requested.connect(self.worker.set_power_unit)
        self.output_panel.output_path_requested.connect(self.worker.set_output_path)

        self.sweep_panel.start_requested.connect(self._on_sweep_start)
        self.sweep_panel.stop_requested.connect(self.worker.stop_sweep)
        self.sweep_panel.pause_requested.connect(self.worker.pause_sweep)
        self.sweep_panel.continue_requested.connect(self.worker.continue_sweep)
        self.sweep_panel.check_requested.connect(self._on_sweep_check)

        # worker -> GUI
        self.worker.resources_listed.connect(self.connection_panel.set_resources)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.error.connect(self._on_error)
        self.worker.status.connect(self._on_status)
        self.worker.state_polled.connect(self._on_state)
        self.worker.sweep_running_changed.connect(self.sweep_panel.set_sweep_running)
        self.worker.sweep_checked.connect(self.sweep_panel.show_check_result)

        self.laser_on_requested.connect(self.worker.set_laser_on)
        self.sweep_start_requested.connect(self.worker.start_sweep)
        self.sweep_configure_requested.connect(self.worker.configure_sweep)

        self.log_panel.debug_check.toggled.connect(self._set_debug_logging)

    def _install_log_handler(self) -> None:
        self.log_handler = QtLogHandler()
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )
        self.log_handler.bridge.message.connect(self.log_panel.append)
        package_logger = logging.getLogger("agilent_8164b")
        package_logger.addHandler(self.log_handler)
        package_logger.setLevel(logging.INFO)

    # -- worker helpers ------------------------------------------------
    def _invoke_worker(self, slot_name: str) -> None:
        """Queue a no-argument worker slot from the GUI thread."""
        QMetaObject.invokeMethod(
            self.worker, slot_name, Qt.ConnectionType.QueuedConnection
        )

    def worker_list_resources(self) -> None:
        self._invoke_worker("list_resources")

    # -- slots ---------------------------------------------------------
    def _on_connected(self, idn: str) -> None:
        self._connected = True
        self.connection_panel.set_connected(True, idn)
        self._set_controls_enabled(True)
        self._on_status(f"Connected — {idn}")

    def _on_disconnected(self) -> None:
        self._connected = False
        self.connection_panel.set_connected(False)
        self._set_controls_enabled(False)
        self.readout_panel.clear()
        self.output_panel.set_laser_state_silently(False)

    def _on_error(self, message: str) -> None:
        logger.error("%s", message)
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #c62828;")
        if not self._connected:
            # Setup-time failures are easy to miss in the status bar, and the
            # user cannot do anything else until they are resolved.
            QMessageBox.warning(self, "Instrument error", message)

    def _on_status(self, message: str) -> None:
        logger.info("%s", message)  # also lands in the Log tab
        self.status_label.setText(message)
        self.status_label.setStyleSheet("")

    def _on_state(self, state: dict) -> None:
        self.readout_panel.update_state(state)
        self.output_panel.set_laser_state_silently(bool(state.get("laser_on")))

    def _on_laser_toggled(self, on: bool) -> None:
        if on and self._confirm_output:
            answer = QMessageBox.question(
                self,
                "Enable laser output?",
                "This will emit laser radiation from the module output.\n\n"
                "Check that the fibre is connected and the path is safe.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                self.output_panel.set_laser_state_silently(False)
                return
        self.laser_on_requested.emit(on)

    def _on_sweep_start(self, config) -> None:
        self.sweep_start_requested.emit(config)

    def _on_sweep_check(self, config) -> None:
        self.sweep_configure_requested.emit(config)
        self._invoke_worker("check_sweep")

    def _emergency_off(self) -> None:
        if not self._connected:
            return
        self._invoke_worker("stop_sweep")
        self.laser_on_requested.emit(False)
        self.output_panel.set_laser_state_silently(False)

    def _reset_instrument(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset instrument?",
            "Send *RST? This returns the module to its default state and "
            "disables the output.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self._invoke_worker("reset")

    def _set_confirm_output(self, enabled: bool) -> None:
        self._confirm_output = enabled

    def _set_debug_logging(self, enabled: bool) -> None:
        logging.getLogger("agilent_8164b").setLevel(
            logging.DEBUG if enabled else logging.INFO
        )

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.output_panel.setEnabled(enabled)
        self.sweep_panel.setEnabled(enabled)
        for action in (self.laser_off_action, self.reset_action, self.errors_action):
            action.setEnabled(enabled)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About",
            "<b>Agilent 8164B laser control</b><br><br>"
            "PyQt6 front end for the Keysight/Agilent 8164B Lightwave "
            "Measurement System tunable laser source.<br><br>"
            "All instrument I/O runs on a worker thread, so the interface "
            "stays responsive during a sweep.",
        )

    # -- settings and shutdown ----------------------------------------
    def _settings(self) -> QSettings:
        return QSettings("combs-lab", "agilent-8164b-gui")

    def _restore_settings(self) -> None:
        settings = self._settings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        resource = settings.value("connection/resource", "")
        if resource:
            self.connection_panel.resource_combo.setCurrentText(resource)
        self.connection_panel.slot_spin.setValue(
            int(settings.value("connection/slot", 0))
        )
        self.connection_panel.channel_spin.setValue(
            int(settings.value("connection/channel", 1))
        )
        self.sweep_panel.start_spin.setValue(
            float(settings.value("sweep/start_nm", 1520.0))
        )
        self.sweep_panel.stop_spin.setValue(
            float(settings.value("sweep/stop_nm", 1580.0))
        )

    def _save_settings(self) -> None:
        settings = self._settings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("connection/resource",
                          self.connection_panel.resource_combo.currentText())
        settings.setValue("connection/slot", self.connection_panel.slot_spin.value())
        settings.setValue("connection/channel", self.connection_panel.channel_spin.value())
        settings.setValue("sweep/start_nm", self.sweep_panel.start_spin.value())
        settings.setValue("sweep/stop_nm", self.sweep_panel.stop_spin.value())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Never leave the lab with the output still on."""
        if self._connected:
            answer = QMessageBox.question(
                self,
                "Quit?",
                "Closing will stop any sweep, disable the output and close "
                "the VISA session.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok,
            )
            if answer != QMessageBox.StandardButton.Ok:
                event.ignore()
                return
            # Block until the worker has actually switched the laser off,
            # otherwise the process can exit with the output still enabled.
            QMetaObject.invokeMethod(
                self.worker, "disconnect_from",
                Qt.ConnectionType.BlockingQueuedConnection,
            )

        self._save_settings()
        logging.getLogger("agilent_8164b").removeHandler(self.log_handler)
        self.thread.quit()
        if not self.thread.wait(5000):
            logger.warning("Worker thread did not stop cleanly; terminating.")
            self.thread.terminate()
            self.thread.wait(1000)
        event.accept()
