"""Live wavelength/power trace, plus CSV export of whatever is on screen.

pyqtgraph is used because it redraws fast enough to keep up with a sweep
being polled several times a second. It is an optional dependency: without
it the panel still collects and exports data, it just cannot draw it.
"""

from __future__ import annotations

import csv
import logging

from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the machine's install
    import pyqtgraph as pg

    HAVE_PYQTGRAPH = True
except ImportError:  # pragma: no cover
    pg = None
    HAVE_PYQTGRAPH = False


class SweepPlot(QWidget):
    """Scatter/line trace of measured power against wavelength."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._wavelengths_nm: list[float] = []
        self._powers: list[float] = []
        self._power_unit = "dBm"

        self.point_count_label = QLabel("0 points")
        self.clear_button = QPushButton("Clear")
        self.export_button = QPushButton("Export CSV…")
        self.export_button.setEnabled(False)

        layout = QVBoxLayout(self)
        if HAVE_PYQTGRAPH:
            pg.setConfigOptions(antialias=True)
            self.plot_widget = pg.PlotWidget()
            # Units go in the label text rather than pyqtgraph's `units=`,
            # which would rescale a 1550 nm axis into "knm".
            self.plot_widget.setLabel("bottom", "Wavelength (nm)")
            self.plot_widget.setLabel("left", f"Power ({self._power_unit})")
            for axis in ("bottom", "left"):
                self.plot_widget.getAxis(axis).enableAutoSIPrefix(False)
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.curve = self.plot_widget.plot(pen=pg.mkPen(width=1.5))
            layout.addWidget(self.plot_widget, 1)
        else:
            self.plot_widget = None
            self.curve = None
            placeholder = QLabel(
                "pyqtgraph is not installed, so the live trace cannot be drawn.\n"
                "Install it with:  pip install pyqtgraph\n\n"
                "Data is still being collected and can be exported to CSV."
            )
            placeholder.setWordWrap(True)
            placeholder.setStyleSheet("color: palette(mid); padding: 24px;")
            layout.addWidget(placeholder, 1)

        controls = QHBoxLayout()
        controls.addWidget(self.point_count_label)
        controls.addStretch(1)
        controls.addWidget(self.clear_button)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)

        self.clear_button.clicked.connect(self.clear)
        self.export_button.clicked.connect(self.export_csv)

    # -- data ----------------------------------------------------------
    def add_sample(self, wavelength_nm: float, power: float) -> None:
        self._wavelengths_nm.append(wavelength_nm)
        self._powers.append(power)
        if self.curve is not None:
            self.curve.setData(self._wavelengths_nm, self._powers)
        self.point_count_label.setText(f"{len(self._powers)} points")
        self.export_button.setEnabled(True)

    def set_power_unit(self, unit: str) -> None:
        """Changing units mid-trace would mix scales, so start a fresh one."""
        if unit == self._power_unit:
            return
        self._power_unit = unit
        if self.plot_widget is not None:
            self.plot_widget.setLabel("left", f"Power ({unit})")
        self.clear()

    def clear(self) -> None:
        self._wavelengths_nm.clear()
        self._powers.clear()
        if self.curve is not None:
            self.curve.setData([], [])
        self.point_count_label.setText("0 points")
        self.export_button.setEnabled(False)

    def export_csv(self) -> None:
        if not self._powers:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export sweep data", "wavelength_scan.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["wavelength_nm", f"power_{self._power_unit}"])
                writer.writerows(zip(self._wavelengths_nm, self._powers))
        except OSError as exc:
            logger.error("Could not write %s: %s", path, exc)
            return
        logger.info("Wrote %d rows to %s", len(self._powers), path)
