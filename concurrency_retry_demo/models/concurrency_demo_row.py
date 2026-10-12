from odoo import api, fields, models


class ConcurrencyDemoRow(models.Model):
    """A row the scenarios fight over."""

    _name = "concurrency.demo.row"
    _description = "Concurrency Demo Row"
    _order = "scenario_id, sequence, id"

    scenario_id = fields.Many2one(
        "concurrency.demo.scenario", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    key = fields.Char(help="Technical key the scenario code uses to find the row.")
    code = fields.Char(help="Business key, unique across all rows.")
    value = fields.Integer()
    processed = fields.Boolean(help="Set by the batched cron job.")
    ledger_ids = fields.One2many("concurrency.demo.ledger", "row_id")
    ledger_total = fields.Integer(compute="_compute_totals")
    total = fields.Integer(
        compute="_compute_totals", help="Value plus the insert-only ledger entries."
    )

    _code_unique = models.Constraint(
        "UNIQUE(code)", "Another row already uses this code."
    )

    @api.depends("value", "ledger_ids.amount")
    def _compute_totals(self):
        for row in self:
            row.ledger_total = sum(row.ledger_ids.mapped("amount"))
            row.total = row.value + row.ledger_total
