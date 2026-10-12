from odoo import fields, models


class ConcurrencyDemoExternalCall(models.Model):
    """A call received by the fake external system (email server, API, ...).

    Written through an independent cursor: like a real external system, it
    keeps every call, whatever happens to the transaction that made it.
    """

    _name = "concurrency.demo.external.call"
    _description = "Concurrency Demo External Call"
    _order = "id desc"

    run_id = fields.Many2one("concurrency.demo.run", ondelete="cascade", index=True)
    scenario_id = fields.Many2one(related="run_id.scenario_id", store=True)
    attempt_no = fields.Integer(string="Sent during attempt")
    channel = fields.Selection(
        selection=[
            ("inline", "Inline, inside the transaction"),
            ("postcommit", "Post-commit hook"),
            ("outbox", "Outbox delivery"),
        ],
        required=True,
    )
    payload = fields.Char()
    idempotency_key = fields.Char(index=True)
    is_duplicate = fields.Boolean(
        string="Ignored as duplicate",
        help="The external system had already applied a call with this "
        "idempotency key, so it ignored this one.",
    )
    received_at = fields.Datetime(default=fields.Datetime.now)
