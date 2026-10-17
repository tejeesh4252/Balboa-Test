from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QVBoxLayout,
)

from import_export_service import _parse_amount


class EditTransactionDialog(QDialog):
    """Edit an already-archived transaction_history row directly. Used
    for fixing a bad legacy-import row or a historical mis-mapping --
    changes here feed the training corpus the moment the mapping engine
    is refreshed, so getting the GL code right matters more here than
    in a one-off correction."""

    def __init__(self, parent, config, row: tuple):
        super().__init__(parent)
        self.config = config
        (self.row_id, fund_name, property_code, sheet_name, bank_name,
         account_number, trans_date, post_month, amount, bank_desc,
         bank_desc2, combined_desc, mapped_gl, mapped_gl_name, bank_gl,
         archived_on, source_month) = row

        self.fields: dict = {}
        self.new_fund_name: str | None = None

        self.setWindowTitle(f"Edit transaction #{self.row_id}")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        meta = QLabel(
            f"Sheet: {sheet_name}   Bank: {bank_name}   Account: {account_number}   "
            f"Archived: {archived_on}")
        meta.setProperty("role", "muted")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        form = QFormLayout()

        self.fund_combo = QComboBox()
        self.fund_combo.addItems(config.fund_names)
        self.fund_combo.setCurrentText(fund_name)
        form.addRow("Fund:", self.fund_combo)

        self.desc_edit = QLineEdit(bank_desc or "")
        form.addRow("Description:", self.desc_edit)

        self.desc2_edit = QLineEdit(bank_desc2 or "")
        form.addRow("Description 2:", self.desc2_edit)

        self.combined_edit = QLineEdit(combined_desc or "")
        form.addRow("Combined (used for matching):", self.combined_edit)

        self.gl_edit = QLineEdit(mapped_gl or "")
        form.addRow("GL code:", self.gl_edit)

        self.gl_name_edit = QLineEdit(mapped_gl_name or "")
        form.addRow("GL name:", self.gl_name_edit)

        self.bank_gl_edit = QLineEdit(bank_gl or "")
        form.addRow("Bank GL:", self.bank_gl_edit)

        self.amount_edit = QLineEdit(str(amount) if amount is not None else "")
        form.addRow("Amount:", self.amount_edit)

        self.date_edit = QLineEdit(trans_date or "")
        form.addRow("Trans date:", self.date_edit)

        self.month_edit = QLineEdit(source_month or "")
        form.addRow("Source month:", self.month_edit)

        layout.addLayout(form)

        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #e74c3c;")
        self.error_lbl.setWordWrap(True)
        layout.addWidget(self.error_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        amount_val = _parse_amount(self.amount_edit.text())
        if amount_val is None:
            self.error_lbl.setText("Amount must be a valid number.")
            return

        if not self.gl_edit.text().strip():
            reply = QMessageBox.question(
                self, "No GL code",
                "This row will have no GL code, which drops it out of "
                "future auto-mapping training entirely. Save anyway?")
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.new_fund_name = self.fund_combo.currentText()
        self.fields = {
            "fund_name": self.new_fund_name,
            "bank_desc": self.desc_edit.text().strip(),
            "bank_desc2": self.desc2_edit.text().strip(),
            "combined_desc": self.combined_edit.text().strip(),
            "mapped_gl": self.gl_edit.text().strip(),
            "mapped_gl_name": self.gl_name_edit.text().strip(),
            "bank_gl": self.bank_gl_edit.text().strip(),
            "amount": amount_val,
            "trans_date": self.date_edit.text().strip(),
            "source_month": self.month_edit.text().strip(),
        }
        self.accept()
