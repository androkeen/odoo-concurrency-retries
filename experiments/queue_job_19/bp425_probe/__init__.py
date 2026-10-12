from psycopg2 import errors

from odoo import api, models
from odoo.exceptions import ConcurrencyError

COUNTS = {}


class Bp425Probe(models.AbstractModel):
    _name = "bp425.probe"
    _description = "BP-425 queue_job probe"

    @api.model
    def fail_serialization(self, key):
        COUNTS[key] = COUNTS.get(key, 0) + 1
        error = errors.SerializationFailure()
        error.__setstate__({"pgcode": "40001"})
        raise error

    @api.model
    def fail_concurrency_error(self, key):
        COUNTS[key] = COUNTS.get(key, 0) + 1
        raise ConcurrencyError("probe: someone was faster")

    @api.model
    def get_count(self, key):
        return COUNTS.get(key, 0)

    @api.model
    def enqueue(self, max_retries=3):
        serialization = self.with_delay(max_retries=max_retries).fail_serialization("qj_40001")
        concurrency = self.with_delay(max_retries=max_retries).fail_concurrency_error("qj_concurrency")
        return [serialization.db_record().uuid, concurrency.db_record().uuid]
