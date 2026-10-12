from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

ROW_KEYS = {
    "serialization": {"target"},
    "deadlock": {"a", "b"},
    "lock_not_available": {"target"},
    "exhaust": {"target"},
    "unique_violation": set(),
    "unsafe_side_effect": {"order", "stats"},
    "safe_side_effect": {"order"},
    "counter": {"counter"},
    "cron": {"target", "batch_1", "batch_2", "batch_3"},
    "upload": {"target"},
}


@tagged("post_install", "-at_install")
class TestDemoData(TransactionCase):
    def test_one_scenario_per_kind(self):
        Scenario = self.env["concurrency.demo.scenario"]
        kinds = [kind for kind, _label in Scenario._fields["kind"].selection]
        self.assertCountEqual(Scenario.search([]).mapped("kind"), kinds)

    def test_each_scenario_has_the_rows_its_buttons_use(self):
        # Rows without a key are created by the runs themselves.
        for scenario in self.env["concurrency.demo.scenario"].search([]):
            with self.subTest(kind=scenario.kind):
                keys = {key for key in scenario.row_ids.mapped("key") if key}
                self.assertEqual(keys, ROW_KEYS[scenario.kind])

    def test_scheduled_actions_only_run_on_demand(self):
        scenario = self.env.ref("concurrency_retry_demo.scenario_cron")
        crons = scenario.cron_id | scenario.batched_cron_id
        self.assertEqual(len(crons), 2)
        for cron in crons:
            self.assertTrue(cron.active)
            self.assertGreater(cron.nextcall, fields.Datetime.now() + relativedelta(years=5))

    def test_reset_puts_the_rows_back_to_zero(self):
        scenario = self.env.ref("concurrency_retry_demo.scenario_counter")
        counter = scenario.row_ids
        counter.value = 7
        self.env["concurrency.demo.ledger"].create({"row_id": counter.id})
        scenario.action_reset()
        self.assertEqual(counter.value, 0)
        self.assertEqual(counter.total, 0)
