import types

from psycopg2 import errors

from odoo import api, models
from odoo.service import model as service_model

# Scratch experiment only: make retrying() back off for 10 ms instead of up
# to 2 ** tryno seconds, so a doubly retried call finishes quickly.
service_model.random = types.SimpleNamespace(uniform=lambda low, high: 0.01)

COUNTS = {}


class Bp425Probe(models.AbstractModel):
    _name = "bp425.probe"
    _description = "BP-425 retry probe"

    @api.model
    def fail_always(self, key):
        COUNTS[key] = COUNTS.get(key, 0) + 1
        error = errors.SerializationFailure()
        error.__setstate__({"pgcode": "40001"})
        raise error

    @api.model
    def get_count(self, key):
        return COUNTS.get(key, 0)

    @api.model
    def _cron_fail_always(self):
        self.fail_always("cron")
