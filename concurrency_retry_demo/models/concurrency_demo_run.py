from odoo import api, fields, models

OUTCOMES = [
    ("running", "Running"),
    ("committed", "Committed"),
    ("partial", "Partly committed"),
    ("failed", "Failed"),
]


class ConcurrencyDemoRun(models.Model):
    """One click on a scenario button: one call, or several simulated requests.

    Written through an independent cursor, like the attempts it groups.
    """

    _name = "concurrency.demo.run"
    _description = "Concurrency Demo Run"
    _order = "id desc"

    scenario_id = fields.Many2one(
        "concurrency.demo.scenario", required=True, ondelete="cascade", index=True
    )
    variant = fields.Char()
    channel = fields.Selection(
        selection=[
            ("http", "HTTP request"),
            ("rpc", "Simulated RPC requests"),
            ("cron", "Scheduled action"),
        ],
        required=True,
        default="http",
    )
    request_count = fields.Integer(string="Requests", default=1)
    attempt_ids = fields.One2many("concurrency.demo.attempt", "run_id")
    external_call_ids = fields.One2many("concurrency.demo.external.call", "run_id")
    attempt_count = fields.Integer(compute="_compute_stats", string="Attempts")
    retry_count = fields.Integer(
        compute="_compute_stats",
        string="Retries",
        help="Failed attempts that were run again.",
    )
    external_call_count = fields.Integer(compute="_compute_stats", string="External calls")
    outcome = fields.Selection(selection=OUTCOMES, compute="_compute_stats")
    value_before = fields.Integer()
    value_after = fields.Integer()
    note = fields.Char()

    @api.depends("scenario_id", "variant")
    def _compute_display_name(self):
        for run in self:
            run.display_name = f"#{run.id or ''} {run.variant or run.scenario_id.name or ''}"

    @api.depends(
        "attempt_ids.state",
        "attempt_ids.retried",
        "attempt_ids.request_no",
        "external_call_ids",
        "request_count",
    )
    def _compute_stats(self):
        for run in self:
            attempts = run.attempt_ids
            run.attempt_count = len(attempts)
            run.retry_count = sum(
                len(self._failed_and_run_again(request_attempts))
                for request_attempts in attempts.grouped("request_no").values()
            )
            run.external_call_count = len(run.external_call_ids)
            run.outcome = run._get_outcome()

    @staticmethod
    def _failed_and_run_again(attempts):
        """Rolled-back attempts of one request that another attempt followed."""
        failed = attempts.filtered(lambda a: a.state == "rolled_back")
        return failed - attempts.sorted("attempt_no")[-1:]

    def _get_outcome(self):
        self.ensure_one()
        states = []
        for attempts in self.attempt_ids.grouped("request_no").values():
            last = attempts.sorted("attempt_no")[-1:]
            if "committed" in attempts.mapped("state"):
                states.append("committed")
            elif last.state == "running" or last.retried:
                states.append("running")
            else:
                states.append("failed")
        if not states or "running" in states or len(states) < self.request_count:
            return "running"
        if all(state == "committed" for state in states):
            return "committed"
        if "committed" in states:
            return "partial"
        return "failed"
