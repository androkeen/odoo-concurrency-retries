{
    "name": "Concurrency Retry Demo",
    "summary": "Make Odoo's automatic retries visible: which errors are retried, "
    "what a retry re-runs, and how to keep side effects retry-safe",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "author": "VentorTech",
    "license": "LGPL-3",
    "depends": ["web"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "data/concurrency_demo_scenario_data.xml",
        "views/concurrency_demo_attempt_views.xml",
        "views/concurrency_demo_run_views.xml",
        "views/concurrency_demo_external_call_views.xml",
        "views/concurrency_demo_scenario_views.xml",
        "views/concurrency_retry_demo_templates.xml",
        "views/concurrency_retry_demo_menus.xml",
    ],
    "installable": True,
}
