from odoo import fields, models


class ConcurrencyDemoOutbox(models.Model):
    """A message to the outside world, written in the business transaction.

    It commits or rolls back together with the business data, so a retried
    attempt leaves no message behind. A worker delivers it afterwards, at
    least once, with its idempotency key.
    """

    _name = "concurrency.demo.outbox"
    _description = "Concurrency Demo Outbox Message"
    _order = "id desc"

    scenario_id = fields.Many2one(
        "concurrency.demo.scenario", required=True, ondelete="cascade", index=True
    )
    run_id = fields.Many2one("concurrency.demo.run", ondelete="set null")
    payload = fields.Char(required=True)
    idempotency_key = fields.Char(required=True, index=True)
    state = fields.Selection(
        selection=[("pending", "Pending"), ("sent", "Sent")],
        default="pending",
        required=True,
    )
    sent_at = fields.Datetime()
