from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from logging_setup import get_logger
from ui.edit_transaction_dialog import EditTransactionDialog

_logger = get_logger("db_viewer")


def _stat_card(label: str, value: str, color: str = "#3b82f6") -> QWidget:
    card = QWidget()
    card.setProperty("role", "card")
    card.setStyleSheet(f"QWidget[role='card'] {{ background:#252d3d; border-radius:8px; }}")
    layout = QVBoxLayout(card)
    val_lbl = QLabel(value)
    val_lbl.setStyleSheet(f"font-size:20px; font-weight:700; color:{color};")
    lbl = QLabel(label)
    lbl.setStyleSheet("color:#8892a4; font-size:11px;")
    layout.addWidget(val_lbl)
    layout.addWidget(lbl)
    return card


class DbViewerDialog(QDialog):

    def __init__(self, parent, config, db, mapper):
        super().__init__(parent)
        self.config = config
        self.db = db
        self.mapper = mapper
        self.setWindowTitle("Database viewer")
        self.setMinimumSize(1000, 640)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_summary_tab(), "Summary")
        tabs.addTab(self._build_history_tab(), "Transaction history")

    # -- summary ------------------------------------------------------
    def _build_summary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        stats = self.db.get_stats()

        grid = QGridLayout()
        cards = [
            ("Total transactions", f"{stats['total']:,}", "#3b82f6"),
            ("Months archived", f"{stats['months']}", "#00d084"),
            ("Funds tracked", f"{stats['funds']}", "#f39c12"),
            ("Unique GL accounts", f"{stats['accts']}", "#a855f7"),
            ("Corrections saved", f"{stats['corrections']}", "#e74c3c"),
        ]
        for i, (label, value, color) in enumerate(cards):
            grid.addWidget(_stat_card(label, value, color), 0, i)
        layout.addLayout(grid)

        layout.addWidget(QLabel(f"Date range: {stats['earliest']} to {stats['latest']}"))

        lower = QHBoxLayout()

        fund_box = QWidget()
        fund_layout = QVBoxLayout(fund_box)
        fund_layout.addWidget(QLabel("Fund breakdown"))
        for fname, cnt in stats["fund_rows"]:
            row = QHBoxLayout()
            row.addWidget(QLabel(fname))
            row.addStretch()
            row.addWidget(QLabel(f"{cnt:,} records"))
            fund_layout.addLayout(row)
        fund_layout.addStretch()
        lower.addWidget(fund_box)

        gl_box = QWidget()
        gl_layout = QVBoxLayout(gl_box)
        gl_layout.addWidget(QLabel("Top GL offset accounts"))
        for gl, cnt in stats["top_gl"]:
            row = QHBoxLayout()
            row.addWidget(QLabel(gl))
            row.addStretch()
            row.addWidget(QLabel(f"{cnt}x"))
            gl_layout.addLayout(row)
        gl_layout.addStretch()
        lower.addWidget(gl_box)

        layout.addLayout(lower)
        layout.addStretch()
        return w

    # -- transaction history -------------------------------------------
    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Fund:"))
        self.fund_combo = QComboBox()
        self.fund_combo.addItems(["All"] + list({r[0] for r in self.db.get_stats()["fund_rows"]}))
        filter_row.addWidget(self.fund_combo)

        filter_row.addWidget(QLabel("Month:"))
        self.month_combo = QComboBox()
        self.month_combo.addItems(["All"] + self.db.list_source_months())
        filter_row.addWidget(self.month_combo)

        filter_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        filter_row.addWidget(self.search_edit, 1)

        filter_btn = QPushButton("Filter")
        filter_btn.clicked.connect(self._refresh)
        filter_row.addWidget(filter_btn)
        layout.addLayout(filter_row)

        self.table = QTableWidget()
        cols = ["ID", "Fund", "Sheet", "Bank", "Account", "Date", "Post month",
                "Amount", "Description", "Description 2", "GL", "GL name",
                "Archived on", "Source month"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit_selected())
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        edit_btn = QPushButton("Edit selected row")
        edit_btn.clicked.connect(self._edit_selected)
        btn_row.addWidget(edit_btn)

        del_btn = QPushButton("Delete selected row")
        del_btn.setProperty("role", "danger")
        del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

        self._refresh()
        return w

    def _refresh(self) -> None:
        rows = self.db.query_transactions(
            self.fund_combo.currentText(), self.month_combo.currentText(),
            self.search_edit.text())
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(str(val)))

    def _selected_row_id(self) -> int | None:
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Select a row first.")
            return None
        return int(self.table.item(selected[0].row(), 0).text())

    def _edit_selected(self) -> None:
        row_id = self._selected_row_id()
        if row_id is None:
            return
        row = self.db.get_transaction(row_id)
        if not row:
            QMessageBox.warning(self, "Not found", "That row no longer exists -- refreshing.")
            self._refresh()
            return

        dlg = EditTransactionDialog(self, self.config, row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self.db.backup(tag="edit_transaction")
        self.db.update_transaction(row_id, dlg.fields)
        _logger.info("Edited transaction #%s: %s", row_id, dlg.fields)

        old_fund = row[1]
        for fund_name in {old_fund, dlg.new_fund_name}:
            if fund_name:
                self.mapper.refresh(fund_name)

        self._refresh()

    def _delete_selected(self) -> None:
        row_id = self._selected_row_id()
        if row_id is None:
            return
        row = self.db.get_transaction(row_id)
        reply = QMessageBox.question(self, "Confirm delete", f"Delete transaction ID {row_id}?")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.backup(tag="delete_transaction")
            self.db.delete_transaction(row_id)
            _logger.info("Deleted transaction #%s", row_id)
            if row:
                self.mapper.refresh(row[1])
            self._refresh()
