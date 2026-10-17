from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from import_export_service import CashbookGLWriter, ETLExporter, MonthArchiver, MonthRollover, TransactionReader
from logging_setup import get_logger, log_bridge
from ui.correction_dialog import CorrectionDialog
from ui.db_viewer import DbViewerDialog
from ui.import_panel import ImportPanel
from ui.legacy_import_dialog import LegacyImportDialog
from ui.settings_dialog import SettingsDialog
from workers import MappingWorker

CONF_COLORS = {"HIGH": "#00d084", "MEDIUM": "#f39c12", "LOW": "#e74c3c", "EXISTING": "#3b82f6"}

_logger = get_logger("main_window")


def _stat_card(layout: QGridLayout, col: int, label: str, color: str):
    box = QWidget()
    box.setProperty("role", "card")
    v = QVBoxLayout(box)
    value_lbl = QLabel("0")
    value_lbl.setStyleSheet(f"font-size:20px; font-weight:700; color:{color};")
    name_lbl = QLabel(label)
    name_lbl.setStyleSheet("color:#8892a4; font-size:11px;")
    v.addWidget(value_lbl)
    v.addWidget(name_lbl)
    layout.addWidget(box, 0, col)
    return value_lbl


class MainWindow(QMainWindow):

    def __init__(self, config, db, mapper):
        super().__init__()
        self.config = config
        self.db = db
        self.mapper = mapper
        self.reader = TransactionReader()
        self.exporter = ETLExporter()
        self.gl_writer = CashbookGLWriter()

        self.transactions: list[dict] = []
        self.current_cashbook_path: str | None = None
        self._mapping_worker = None

        self.setWindowTitle("Balboa mapping agent")
        self.resize(1280, 860)
        self._build_ui()
        self._refresh_db_stat()

    # -- layout --------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Fund:"))
        self.fund_combo = QComboBox()
        self.fund_combo.addItems(self.config.fund_names)
        self.fund_combo.currentTextChanged.connect(self._on_fund_changed)
        top_row.addWidget(self.fund_combo)
        top_row.addStretch()

        settings_btn = QPushButton("Settings")
        settings_btn.setProperty("role", "secondary")
        settings_btn.clicked.connect(self._open_settings)
        top_row.addWidget(settings_btn)

        db_viewer_btn = QPushButton("Database viewer")
        db_viewer_btn.setProperty("role", "secondary")
        db_viewer_btn.clicked.connect(self._open_db_viewer)
        top_row.addWidget(db_viewer_btn)

        import_btn = QPushButton("Import bank file...")
        import_btn.setProperty("role", "purple")
        import_btn.clicked.connect(self._open_import_panel)
        top_row.addWidget(import_btn)

        legacy_btn = QPushButton("Import legacy training data...")
        legacy_btn.setProperty("role", "secondary")
        legacy_btn.clicked.connect(self._open_legacy_import)
        top_row.addWidget(legacy_btn)

        rollover_btn = QPushButton("Start new month...")
        rollover_btn.setProperty("role", "secondary")
        rollover_btn.clicked.connect(self._open_month_rollover)
        top_row.addWidget(rollover_btn)
        root.addLayout(top_row)

        stats_row = QGridLayout()
        self.stat_total = _stat_card(stats_row, 0, "Loaded transactions", "#e8ecf3")
        self.stat_high = _stat_card(stats_row, 1, "High confidence", CONF_COLORS["HIGH"])
        self.stat_medium = _stat_card(stats_row, 2, "Medium confidence", CONF_COLORS["MEDIUM"])
        self.stat_low = _stat_card(stats_row, 3, "Low confidence", CONF_COLORS["LOW"])
        self.stat_db = _stat_card(stats_row, 4, "Total in database", "#a855f7")
        root.addLayout(stats_row)

        action_row = QHBoxLayout()
        upload_btn = QPushButton("Upload monthly file")
        upload_btn.clicked.connect(self._upload_file)
        action_row.addWidget(upload_btn)

        self.map_btn = QPushButton("Run auto-mapping")
        self.map_btn.clicked.connect(self._run_mapping)
        action_row.addWidget(self.map_btn)

        save_gl_btn = QPushButton("Save GL to cashbook")
        save_gl_btn.setProperty("role", "secondary")
        save_gl_btn.clicked.connect(self._save_gl_to_cashbook)
        action_row.addWidget(save_gl_btn)

        archive_btn = QPushButton("Archive reviewed batch")
        archive_btn.setProperty("role", "secondary")
        archive_btn.clicked.connect(self._archive_batch)
        action_row.addWidget(archive_btn)

        export_btn = QPushButton("Export Yardi ETL")
        export_btn.setProperty("role", "secondary")
        export_btn.clicked.connect(self._export_etl)
        action_row.addWidget(export_btn)
        action_row.addStretch()
        root.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.table = QTableWidget()
        cols = ["Sheet", "Date", "Amount", "Description", "GL", "GL name",
                "Confidence", "Score", "Status"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        root.addWidget(self.table, 1)

        self.status_lbl = QLabel("Ready.")
        self.statusBar().addWidget(self.status_lbl)

    # -- helpers --------------------------------------------------------
    def _set_status(self, msg: str) -> None:
        self.status_lbl.setText(msg)

    def _current_fund(self) -> str:
        return self.fund_combo.currentText()

    def _on_fund_changed(self, fund_name: str) -> None:
        self.mapper.refresh(fund_name)
        self._set_status(f"Switched to {fund_name}. Training corpus: {self.mapper.corpus_size(fund_name)} rows.")

    def _populate_table(self, show_mapping: bool) -> None:
        self.table.setRowCount(len(self.transactions))
        for i, txn in enumerate(self.transactions):
            try:
                date_str = (pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(txn["date"]))).strftime("%m/%d/%Y")
            except Exception:
                date_str = str(txn.get("date", ""))
            amt_str = f"{txn.get('amount', 0):,.2f}"

            gl = name = score = conf = status = ""
            if show_mapping:
                gl = txn.get("mapped_gl", "")
                name = txn.get("mapped_name", "")
                score = str(txn.get("score", ""))
                conf = txn.get("confidence", "")
                status = txn.get("status", "")

            values = [txn.get("sheet", ""), date_str, amt_str,
                      txn.get("combined_desc", ""), gl, name, conf, score, status]
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                color = CONF_COLORS.get(conf if status != "EXISTING" else "EXISTING")
                if color and c == 6:
                    item.setForeground(Qt.GlobalColor.white)
                    item.setBackground(_qcolor(color))
                self.table.setItem(i, c, item)
        self.table.resizeColumnsToContents()

    def _refresh_db_stat(self) -> None:
        try:
            self.stat_db.setText(f"{self.db.get_stats()['total']:,}")
        except Exception:
            pass

    def _filter_already_archived(self, transactions: list, fund_name: str) -> tuple:
        """Drops rows already present in the database for this fund --
        prevents a completed, archived month from reappearing in the
        review table every time the sheet is re-read, while leaving the
        row untouched in the actual Excel file, which is meant to keep
        every month permanently."""
        archived_keys = self.db.get_archived_keys(fund_name)
        kept, removed = [], 0
        for t in transactions:
            key = (str(t.get("date", "")),
                   round(t.get("amount", 0) or 0, 2),
                   str(t.get("bank_desc", "")).strip().lower())
            if key in archived_keys:
                removed += 1
            else:
                kept.append(t)
        return kept, removed

    # -- actions ----------------------------------------------------
    def _upload_file(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(self, "Select monthly cashbook", "", "Excel files (*.xlsx *.xls)")
        if not fp:
            return
        self.current_cashbook_path = fp
        self._set_status(f"Reading {os.path.basename(fp)}...")
        try:
            skip_sheets = self.config.data["skip_sheets"]
            xl = pd.ExcelFile(fp)
            sheets = [s for s in xl.sheet_names if s not in skip_sheets]
            self.transactions = []
            log = log_bridge(logger=_logger)
            for sheet in sheets:
                txns, _ = self.reader.read_sheet(fp, sheet, log=log)
                self.transactions.extend(txns)

            self.transactions, hidden = self._filter_already_archived(
                self.transactions, self._current_fund())

            self.stat_total.setText(f"{len(self.transactions):,}")
            self._populate_table(show_mapping=False)
            hidden_note = f"  |  {hidden} already-archived row(s) hidden" if hidden else ""
            self._set_status(
                f"{os.path.basename(fp)}  |  {len(self.transactions)} rows loaded  |  "
                f"click 'Run auto-mapping'{hidden_note}")
        except Exception as e:
            _logger.exception("Failed to read uploaded file %s", fp)
            QMessageBox.critical(self, "Error", f"Could not read file:\n{e}")
            self._set_status("File load failed.")

    def _run_mapping(self) -> None:
        if not self.transactions:
            QMessageBox.warning(self, "No data", "Upload a monthly file first.")
            return
        self.map_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_status("Running auto-mapping...")

        self._mapping_worker = MappingWorker(self.mapper, self._current_fund(), self.transactions)
        self._mapping_worker.progress.connect(
            lambda done, total: self.progress.setValue(int(done / max(total, 1) * 100)))
        self._mapping_worker.finished_ok.connect(self._on_mapping_finished)
        self._mapping_worker.failed.connect(self._on_mapping_failed)
        self._mapping_worker.start()

    def _on_mapping_finished(self, transactions: list, counts: dict) -> None:
        self.transactions = transactions
        self.map_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.stat_high.setText(str(counts.get("HIGH", 0) + counts.get("EXISTING", 0)))
        self.stat_medium.setText(str(counts.get("MEDIUM", 0)))
        self.stat_low.setText(str(counts.get("LOW", 0)))
        self._populate_table(show_mapping=True)
        self._set_status(
            f"Mapping complete  |  HIGH {counts.get('HIGH', 0)}  MEDIUM {counts.get('MEDIUM', 0)}  "
            f"LOW {counts.get('LOW', 0)}  EXISTING {counts.get('EXISTING', 0)}")

    def _on_mapping_failed(self, message: str) -> None:
        self.map_btn.setEnabled(True)
        self.progress.setVisible(False)
        QMessageBox.critical(self, "Mapping failed", message)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        if row >= len(self.transactions):
            return
        txn = self.transactions[row]
        dlg = CorrectionDialog(
            self, txn, self._current_fund(),
            self.current_cashbook_path or self.config.cashbook_path(self._current_fund()),
            self.mapper, on_applied=lambda: self._populate_table(show_mapping=True))
        dlg.exec()

    def _save_gl_to_cashbook(self) -> None:
        if not self.transactions:
            QMessageBox.warning(self, "No data", "No transactions loaded.")
            return
        if not self.current_cashbook_path:
            QMessageBox.warning(self, "No file", "No cashbook file loaded.")
            return
        mapped = [t for t in self.transactions if t.get("mapped_gl")]
        if not mapped:
            QMessageBox.warning(self, "No mappings", "Run auto-mapping first.")
            return
        try:
            header_rows = self.config.data["header_rows"]
            updated, backup_path = self.gl_writer.save(
                self.current_cashbook_path, mapped, header_rows,
                log=log_bridge(logger=_logger))
            self._set_status(f"Saved {updated} GL mappings to cashbook.")
            QMessageBox.information(
                self, "Saved",
                f"{updated} GL mappings written to:\n{os.path.basename(self.current_cashbook_path)}\n\n"
                f"Backup saved to:\n{backup_path}")
        except Exception as e:
            _logger.exception("Failed to save GL mappings to cashbook %s", self.current_cashbook_path)
            QMessageBox.critical(self, "Error", str(e))

    def _archive_batch(self) -> None:
        if not self.transactions:
            QMessageBox.warning(self, "No data", "No transactions loaded.")
            return
        mapped = [t for t in self.transactions if t.get("mapped_gl") and t.get("status") != "SKIP"]
        if not mapped:
            QMessageBox.warning(self, "Nothing to archive", "No mapped transactions found.")
            return
        try:
            sample_date = pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(mapped[0]["date"]))
            source_month = sample_date.strftime("%Y-%m")
        except Exception:
            source_month = datetime.now().strftime("%Y-%m")

        fund_name = self._current_fund()
        property_code = self.config.property_code(fund_name)
        header_rows = self.config.data["header_rows"]
        archiver = MonthArchiver(self.db, header_rows)
        ins, skp = archiver.archive_reviewed_batch(mapped, fund_name, property_code, source_month)
        self.mapper.refresh(fund_name)
        self._refresh_db_stat()
        self._set_status(f"Archived {ins} rows to database ({skp} already present).")
        QMessageBox.information(
            self, "Archive complete",
            f"{ins} transaction(s) archived to the database for {source_month}.\n"
            f"{skp} were already present.")

    def _export_etl(self) -> None:
        if not self.transactions:
            QMessageBox.warning(self, "No data", "No transactions to export.")
            return
        ready = [t for t in self.transactions if t.get("mapped_gl") and t.get("status") != "SKIP"]
        manual = [t for t in self.transactions if not t.get("mapped_gl") and t.get("status") != "SKIP"]
        if manual:
            reply = QMessageBox.question(
                self, "Unmapped transactions",
                f"{len(manual)} transaction(s) have no GL code.\n\n"
                f"Export the {len(ready)} mapped transactions only?")
            if reply != QMessageBox.StandardButton.Yes:
                return

        fund_name = self._current_fund()
        property_code = self.config.property_code(fund_name)
        book_num = self.config.data["book_num"]
        fp, _ = QFileDialog.getSaveFileName(
            self, "Save Yardi ETL file",
            f"Yardi_ETL_{fund_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.xlsx",
            "Excel files (*.xlsx)")
        if not fp:
            return
        ok, msg = self.exporter.export(ready, fund_name, property_code, book_num, fp)
        if ok:
            QMessageBox.information(self, "Export complete", f"{msg}\n\nSaved to:\n{fp}")
            self._set_status(f"Exported -> {fp}")
        else:
            QMessageBox.critical(self, "Export failed", msg)

    def _open_import_panel(self) -> None:
        dlg = ImportPanel(self, self.config, self.db, self.mapper, self._current_fund())
        dlg.exec()
        self._refresh_db_stat()
        if dlg.last_result:
            self._chain_from_import(dlg.cashbook_path, dlg.last_fund_name, dlg.last_result)

    def _chain_from_import(self, cashbook_path: str | None, fund_name: str | None,
                            result: dict) -> None:
        """Populates the review table straight from a completed bank
        import -- no separate 'Upload monthly file' step re-reading the
        same file that was just imported. The cashbook write itself is
        untouched; this only removes the redundant manual re-read."""
        if not result or result.get("total_added", 0) == 0:
            return
        if fund_name:
            self.fund_combo.setCurrentText(fund_name)

        unmapped_sheets = {u["sheet_name"] for u in result.get("unmapped_accounts", [])}
        imported_sheets = {
            acc["sheet_name"] for acc in result.get("accounts", [])
            if acc["sheet_name"] not in unmapped_sheets
        }
        if not imported_sheets or not cashbook_path:
            return

        self.current_cashbook_path = cashbook_path
        self.transactions = []
        log = log_bridge(logger=_logger)
        for sheet in imported_sheets:
            txns, _ = self.reader.read_sheet(cashbook_path, sheet, log=log)
            self.transactions.extend(txns)

        self.transactions, hidden = self._filter_already_archived(self.transactions, fund_name)

        self.stat_total.setText(f"{len(self.transactions):,}")
        self._populate_table(show_mapping=False)
        hidden_note = f"  |  {hidden} already-archived row(s) hidden" if hidden else ""
        self._set_status(
            f"Loaded {len(self.transactions)} transaction(s) automatically from the "
            f"import -- click 'Run auto-mapping' when ready.{hidden_note}")

    def _open_db_viewer(self) -> None:
        dlg = DbViewerDialog(self, self.config, self.db, self.mapper)
        dlg.exec()

    def _open_legacy_import(self) -> None:
        dlg = LegacyImportDialog(self, self.config, self.db)
        dlg.exec()
        for fund_name in self.config.fund_names:
            self.mapper.refresh(fund_name)
        self._refresh_db_stat()
        self._set_status("Legacy training data refreshed into the mapping engine.")

    def _open_month_rollover(self) -> None:
        fund_name = self._current_fund()
        cashbook_path = self.current_cashbook_path or self.config.cashbook_path(fund_name)
        if not cashbook_path:
            QMessageBox.warning(
                self, "No cashbook",
                "No cashbook file is set for this fund yet. Set one in Settings first.")
            return

        fund_cfg = self.config.fund(fund_name)
        header_rows = self.config.data["header_rows"]
        rollover = MonthRollover(header_rows)

        try:
            unarchived = rollover.check_unarchived(
                cashbook_path, fund_cfg["sheet_mapping"], fund_name, self.db)
        except Exception as e:
            _logger.exception("Failed to check unarchived rows before rollover")
            QMessageBox.critical(self, "Error", str(e))
            return

        warning = ""
        if unarchived:
            lines = "\n".join(f"  {sheet}: {count} row(s)" for sheet, count in unarchived.items())
            warning = (
                f"\n\nWARNING -- these rows are not yet archived in the database:\n{lines}\n\n"
                f"They'll still be fully preserved in the archive snapshot, but once the "
                f"live sheet is cleared they're no longer part of the active mapping "
                f"workflow unless you go find them in the archive later. Consider mapping "
                f"and archiving them first.")

        reply = QMessageBox.question(
            self, "Start new month",
            f"This will, for {fund_name}:\n\n"
            f"1. Save a full snapshot of the current cashbook to an Archive folder "
            f"next to the file\n"
            f"2. Clear all transaction rows from the live cashbook, ready for the "
            f"next month's import"
            f"{warning}\n\nContinue?")
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            archive_path = rollover.roll_over(
                cashbook_path, fund_cfg["sheet_mapping"], log=log_bridge(logger=_logger))
        except Exception as e:
            _logger.exception("Month rollover failed for %s", cashbook_path)
            QMessageBox.critical(
                self, "Rollover failed",
                f"{e}\n\nThe live cashbook was not modified.")
            return

        if self.current_cashbook_path == cashbook_path:
            self.transactions = []
            self._populate_table(show_mapping=False)
            self.stat_total.setText("0")
        self._set_status(f"{fund_name} cashbook rolled over. Archived to {archive_path}")
        QMessageBox.information(
            self, "Rollover complete",
            f"Snapshot saved to:\n{archive_path}\n\n"
            f"The live cashbook is now clean and ready for the new month.")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, self.config)
        dlg.exec()


def _qcolor(hex_str: str):
    from PyQt6.QtGui import QColor
    return QColor(hex_str)
