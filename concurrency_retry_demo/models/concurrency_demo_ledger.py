from odoo import fields, models


class ConcurrencyDemoLedger(models.Model):
    """Insert-only counter: one row per increment, summed when read."""

    _name = "concurrency.demo.ledger"
    _description = "Concurrency Demo Ledger Entry"
    _order = "id desc"

    row_id = fields.Many2one(
        "concurrency.demo.row", required=True, ondelete="cascade", index=True
    )
    run_id = fields.Many2one("concurrency.demo.run", ondelete="set null")
    amount = fields.Integer(default=1)
