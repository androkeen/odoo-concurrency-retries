from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestUploadPage(HttpCase):
    def test_upload_page_renders(self):
        self.authenticate("admin", "admin")
        response = self.url_open("/concurrency_retry_demo/upload")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="csrf_token"', response.text)
        self.assertEqual(response.text.count('type="file"'), 3)
