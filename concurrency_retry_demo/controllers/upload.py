import io

from werkzeug.exceptions import BadRequest

from odoo import http
from odoo.http import request

from ..models.concurrency_demo_scenario import UPLOAD_MODES


class NonSeekableStream(io.RawIOBase):
    """Read-only view of a stream that cannot seek, like a streamed upload."""

    def __init__(self, stream):
        super().__init__()
        self._stream = stream

    def readable(self):
        return True

    def seekable(self):
        return False

    def readinto(self, buffer):
        data = self._stream.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


class ConcurrencyRetryDemoUpload(http.Controller):
    @http.route("/concurrency_retry_demo/upload", type="http", auth="user", methods=["GET"])
    def upload_page(self):
        scenario = request.env.ref("concurrency_retry_demo.scenario_upload")
        return request.render(
            "concurrency_retry_demo.upload_page",
            {"scenario": scenario, "modes": UPLOAD_MODES, "run": scenario.last_run_id},
        )

    @http.route("/concurrency_retry_demo/upload", type="http", auth="user", methods=["POST"])
    def upload_submit(self, mode="seekable", **kwargs):
        if mode not in UPLOAD_MODES:
            raise BadRequest(f"Unknown mode {mode!r}")
        upload = request.httprequest.files.get("upload")
        if not upload:
            raise BadRequest("Choose a file to upload")
        if mode != "seekable":
            upload.stream = NonSeekableStream(upload.stream)
        # This controller runs again on each attempt. retrying() rewinds the
        # uploaded stream in between, or raises RuntimeError when it cannot.
        data = upload.read()
        scenario = request.env.ref("concurrency_retry_demo.scenario_upload")
        scenario._upload_attempt(mode, len(data))
        return request.redirect("/concurrency_retry_demo/upload")
