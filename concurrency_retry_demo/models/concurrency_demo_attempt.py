from odoo import api, fields, models


class ConcurrencyDemoAttempt(models.Model):
    """One execution of a call: what retrying() runs up to five times.

    Written through an independent cursor (tools/attempt_log.py), so the row
    survives the rollback of the attempt it describes.
    """

    _name = "concurrency.demo.attempt"
    _description = "Concurrency Demo Attempt"
    _order = "run_id desc, request_no, attempt_no"

    run_id = fields.Many2one(
        "concurrency.demo.run", required=True, ondelete="cascade", index=True
    )
    scenario_id = fields.Many2one(related="run_id.scenario_id", store=True)
    request_no = fields.Integer(string="Request", default=1)
    attempt_no = fields.Integer(string="Attempt")
    offset_ms = fields.Integer(
        string="Start (ms)", help="Milliseconds between the start of the run and this attempt."
    )
    duration_ms = fields.Integer(string="Duration (ms)")
    state = fields.Selection(
        selection=[
            ("running", "Running"),
            ("committed", "Committed"),
            ("rolled_back", "Rolled back"),
        ],
        default="running",
        required=True,
    )
    error_name = fields.Char(string="Error")
    pgcode = fields.Char(string="SQLSTATE")
    error_type = fields.Char(string="Exception")
    error_message = fields.Text()
    retried = fields.Boolean(help="retrying() logged a retry for this failure.")
    gave_up = fields.Boolean(
        help="retrying() logged 'maximum number of tries reached' after this failure."
    )
    wait_ms = fields.Integer(
        string="Backoff (ms)", help="Sleep retrying() chose before the next attempt."
    )
    retry_log = fields.Char(string="retrying() log line")
    thread_name = fields.Char(string="Thread")
    note = fields.Char()

    @api.depends("run_id", "request_no", "attempt_no")
    def _compute_display_name(self):
        for attempt in self:
            attempt.display_name = self.env._(
                "Run %(run)s, request %(request)s, attempt %(attempt)s",
                run=attempt.run_id.id,
                request=attempt.request_no,
                attempt=attempt.attempt_no,
            )
