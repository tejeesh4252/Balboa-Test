from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from legacy_import import commit_legacy_import, preview_legacy_file
from logging_setup import get_logger

_logger = get_logger("legacy_import_ui")


class _PreviewWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, filepath: str, sheet_name: str, known_funds: list):
        super().__init__()
        self.filepath = filepath
        self.sheet_name = sheet_name
        self.known_funds = known_funds

    def run(self):
        try:
            preview = preview_legacy_file(self.filepath, self.sheet_name, self.known_funds)
            self.done.emit(preview)
        except Exception as e:
            _logger.exception("Legacy preview worker failed")
            self.failed.emit(str(e))


class LegacyImportDialog(QDialog):
    """One-time seed of transaction_history from a consolidated legacy
    training file. The file is picked with this machine's own file
    dialog and read locally -- nothing about it leaves this computer.
    Always previews before writing; nothing is committed until you
    click Confirm."""

    def __init__(self, parent, config, db):
        super().__init__(parent)
        self.config = config
        self.db = db
        self.filepath: str | None = None
        self.preview = None
        self._worker = None

        self.setWindowTitle("Import legacy training data")
        self.setMinimumSize(640, 500)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Select the consolidated training file from this computer. Nothing "
            "is written to the database until you review the preview and click "
            "Confirm."))

        row = QHBoxLayout()
        self.file_btn = QPushButton("Select file...")
        self.file_btn.clicked.connect(self._pick_file)
        self.file_lbl = QLabel("No file selected")
        row.addWidget(self.file_btn)
        row.addWidget(self.file_lbl, 1)
        layout.addLayout(row)

        sheet_row = QHBoxLayout()
        sheet_row.addWidget(QLabel("Sheet name:"))
        self.sheet_edit = QLineEdit("All Funds")
        sheet_row.addWidget(self.sheet_edit)
        layout.addLayout(sheet_row)

        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self._run_preview)
        layout.addWidget(self.preview_btn)

        self.result_box = QPlainTextEdit()
        self.result_box.setReadOnly(True)
        layout.addWidget(self.result_box, 1)

        self.commit_btn = QPushButton("Confirm and import")
        self.commit_btn.setProperty("role", "purple")
        self.commit_btn.setEnabled(False)
        self.commit_btn.clicked.connect(self._commit)
        layout.addWidget(self.commit_btn)

    def _pick_file(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(
            self, "Select legacy training file", "", "Excel files (*.xlsx *.xls)")
        if fp:
            self.filepath = fp
            self.file_lbl.setText(fp)
            self.preview_btn.setEnabled(True)
            self.commit_btn.setEnabled(False)
            self.preview = None
            self.result_box.clear()

    def _run_preview(self) -> None:
        self.preview_btn.setEnabled(False)
        self.commit_btn.setEnabled(False)
        self.result_box.setPlainText("Reading file...")
        self._worker = _PreviewWorker(self.filepath, self.sheet_edit.text().strip(), self.config.fund_names)
        self._worker.done.connect(self._on_preview_done)
        self._worker.failed.connect(self._on_preview_failed)
        self._worker.start()

    def _on_preview_done(self, preview) -> None:
        self.preview_btn.setEnabled(True)
        self.preview = preview

        if preview.errors:
            self.result_box.setPlainText("\n".join(preview.errors))
            self.commit_btn.setEnabled(False)
            return

        lines = [
            f"Sheet: {preview.sheet_name}",
            f"Total rows read: {preview.total_rows}",
            f"Duplicate rows skipped: {preview.duplicate_rows}",
            f"Rows with no GL code skipped: {preview.missing_gl_rows}",
            f"Unique rows ready to import: {preview.unique_rows}",
            "",
            "By fund:",
        ]
        for fund, count in preview.fund_counts.items():
            lines.append(f"  {fund}: {count}")

        if preview.unmatched_funds:
            lines.append("")
            lines.append("WARNING -- these Fund values don't match a configured "
                          "fund and were skipped entirely:")
            for fund, count in preview.unmatched_funds.items():
                lines.append(f"  '{fund}': {count} row(s)")

        self.result_box.setPlainText("\n".join(lines))
        self.commit_btn.setEnabled(preview.unique_rows > 0)

    def _on_preview_failed(self, message: str) -> None:
        self.preview_btn.setEnabled(True)
        self.result_box.setPlainText(f"ERROR: {message}")
        self.commit_btn.setEnabled(False)

    def _commit(self) -> None:
        if not self.preview or not self.preview.rows:
            return
        reply = QMessageBox.question(
            self, "Confirm import",
            f"This writes {self.preview.unique_rows} row(s) into the live "
            f"database. A backup is taken first. Continue?")
        if reply != QMessageBox.StandardButton.Yes:
            return

        property_codes = {f: self.config.property_code(f) for f in self.config.fund_names}
        try:
            results = commit_legacy_import(self.db, self.preview, property_codes)
        except Exception as e:
            _logger.exception("Legacy import commit failed")
            QMessageBox.critical(self, "Import failed", str(e))
            return

        summary = "\n".join(
            f"{fund}: {ins} inserted, {skp} already present"
            for fund, (ins, skp) in results.items())
        QMessageBox.information(self, "Import complete", summary)
        self.commit_btn.setEnabled(False)
        self.accept()
