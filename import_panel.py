from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from import_export_service import BankImporter, MonthArchiver
from workers import ImportWorker


class ImportPanel(QDialog):
    """Reads a raw bank source file, appends new rows to the matching
    cashbook sheets, then auto-archives any prior month that's fully
    GL-mapped -- the connective tissue the original tool was missing."""

    def __init__(self, parent, config, db, mapper, fund_name: str):
        super().__init__(parent)
        self.config = config
        self.db = db
        self.mapper = mapper
        self.source_path = None
        self.cashbook_path = None

        self.setWindowTitle("Import bank file to cashbook")
        self.setMinimumSize(640, 560)

        self.last_result: dict | None = None
        self.last_fund_name: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Reads the bank source file, appends new transactions to the "
            "cashbook, then automatically archives any fully-mapped prior "
            "month to the database."))

        fund_row = QHBoxLayout()
        fund_row.addWidget(QLabel("Fund:"))
        self.fund_combo = QComboBox()
        self.fund_combo.addItems(config.fund_names)
        self.fund_combo.setCurrentText(fund_name)
        fund_row.addWidget(self.fund_combo)
        fund_row.addStretch()
        layout.addLayout(fund_row)

        src_row = QHBoxLayout()
        self.src_btn = QPushButton("Source file...")
        self.src_btn.clicked.connect(self._pick_source)
        self.src_lbl = QLabel("No file selected")
        src_row.addWidget(self.src_btn)
        src_row.addWidget(self.src_lbl, 1)
        layout.addLayout(src_row)

        cash_row = QHBoxLayout()
        self.cash_btn = QPushButton("Cashbook file...")
        self.cash_btn.clicked.connect(self._pick_cashbook)
        self.cash_lbl = QLabel("No file selected")
        cash_row.addWidget(self.cash_btn)
        cash_row.addWidget(self.cash_lbl, 1)
        layout.addLayout(cash_row)

        self.run_btn = QPushButton("Run import")
        self.run_btn.setProperty("role", "purple")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._start_import)
        layout.addWidget(self.run_btn)

        layout.addWidget(QLabel("Import log:"))
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, 1)

        self._worker = None

    def _pick_source(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(self, "Select bank source file", "", "Excel files (*.xlsx *.xls)")
        if fp:
            self.source_path = fp
            self.src_lbl.setText(fp)
            self._check_ready()

    def _pick_cashbook(self) -> None:
        fund_key = self.fund_combo.currentText()
        default = self.config.cashbook_path(fund_key)
        fp, _ = QFileDialog.getOpenFileName(
            self, "Select cashbook file", default, "Excel files (*.xlsx)")
        if fp:
            self.cashbook_path = fp
            self.cash_lbl.setText(fp)
            self._check_ready()

    def _check_ready(self) -> None:
        self.run_btn.setEnabled(bool(self.source_path and self.cashbook_path))

    def _log(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)

    def _start_import(self) -> None:
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Importing...")
        self.log_box.clear()

        fund_name = self.fund_combo.currentText()
        fund_cfg = self.config.fund(fund_name)
        property_code = self.config.property_code(fund_name)
        header_rows = self.config.data["header_rows"]
        exclude_keywords = self.config.data["exclude_keywords"]

        importer = BankImporter(header_rows, exclude_keywords)
        archiver = MonthArchiver(self.db, header_rows)

        self._worker = ImportWorker(
            importer, archiver, self.source_path, self.cashbook_path,
            fund_cfg, fund_name, property_code, fund_cfg["sheet_mapping"])
        self._worker.log.connect(self._log)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_finished(self, result: dict) -> None:
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run import")
        self.last_result = result
        self.last_fund_name = self.fund_combo.currentText()

        archive = result.get("archive", {})
        archived = archive.get("archived_months", [])
        pending = archive.get("pending_months", [])
        unmapped = result.get("unmapped_accounts", [])

        self._log("=" * 50)
        self._log(f"Rows imported: {result['total_added']}  |  duplicates skipped: {result['total_skipped']}")
        if archived:
            for month, ins, skp in archived:
                self._log(f"Auto-archived {month}: {ins} row(s) to database")
        if pending:
            for month, count in pending:
                self._log(f"{month} not archived yet -- {count} row(s) still need GL mapping")

        # Refresh the mapping engine for this fund so any newly-archived
        # month is immediately part of the training corpus.
        self.mapper.refresh(self.fund_combo.currentText())

        if unmapped:
            lines = [
                f"{u['bank_name']} {u['account_number']}: {u['row_count']} "
                f"transaction(s), ${u['total_amount']:,.2f} NOT imported "
                f"(no cashbook sheet '{u['sheet_name']}')"
                for u in unmapped
            ]
            QMessageBox.warning(
                self, "Some accounts were not imported",
                "Rows imported: {added}\nDuplicates skipped: {skipped}\n\n"
                "The following account(s) have no matching cashbook sheet and "
                "were skipped entirely -- add them under Settings, then re-run "
                "the import:\n\n{lines}".format(
                    added=result['total_added'], skipped=result['total_skipped'],
                    lines="\n".join(lines)))
        else:
            QMessageBox.information(
                self, "Import complete",
                f"Rows imported: {result['total_added']}\n"
                f"Duplicates skipped: {result['total_skipped']}\n"
                f"Months auto-archived: {len(archived)}\n"
                f"Months pending manual mapping: {len(pending)}")

    def _on_failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run import")
        self._log(f"ERROR: {message}")
        QMessageBox.critical(self, "Import failed", message)
