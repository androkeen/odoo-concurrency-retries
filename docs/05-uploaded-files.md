# 05 - Uploaded files

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`, werkzeug 3.1.8.
> **Status:** verified in the source and by the demo upload page (three modes); the werkzeug details are source reading.

## TL;DR

- Before a retry, `retrying()` calls `seek(0)` on every file in `request.httprequest.files`, or raises `RuntimeError: Cannot retry request on input file '<field>' after serialization failure` if a stream cannot seek.
- The check runs **before** the error is classified: a unique violation with a non-seekable upload gives the `RuntimeError`, not the `ValidationError`.
- Normal multipart uploads are seekable (werkzeug spools them), so the `RuntimeError` needs a custom stream. A seekable upload is simply read again by every attempt.
- `'<field>'` is the form field name (`'upload'`), not the uploaded file's name.

## Deep dive

### The code

```python
# odoo/service/model.py:198-206, after the rollback and resets
request = http.request
if request:
    request.session = request._get_session_and_dbname()[0]
    # Rewind files in case of failure
    for filename, file in request.httprequest.files.items():
        if hasattr(file, "seekable") and file.seekable():
            file.seek(0)
        else:
            raise RuntimeError(f"Cannot retry request on input file {filename!r} after serialization failure") from exc
```

- It runs inside the `except (IntegrityError, OperationalError, ConcurrencyError)` block, for **every** caught error: retryable or not, and on the fifth attempt too, when no retry follows. That is why the message can show up "after serialization failure" for an error that would never have been retried.
- The `RuntimeError` replaces the original error, which stays reachable as `__cause__` (`from exc`): the traceback shows "The above exception was the direct cause of the following exception".
- It is raised before the retry log line, so a request that dies this way logs no `tries left` line, only the 500.
- `filename` is the key of the `MultiDict`, the name of the form field. The client's file name would be `file.filename`.
- Only requests that have a `http.request` get this treatment. RPC calls go through `dispatch_rpc()`, which hides the request (`borrow_request()`), and they carry no multipart files anyway.

### When is an upload not seekable?

werkzeug parses a multipart body into `FileStorage` objects backed by a `SpooledTemporaryFile` (in memory up to 500 KB, then on disk; `werkzeug.formparser.default_stream_factory`), which is seekable, and `FileStorage` delegates `seekable()` and `seek()` to it. On Python < 3.11, where `SpooledTemporaryFile` has no `seekable()`, `FileStorage.__getattr__` falls back to the backing file, so the rewind works there too. The check fails only when code replaced the stream: a custom stream factory or form parser, or a wrapper around the stream. The demo does exactly that to show the path: it wraps the stream in an `io.RawIOBase` whose `seekable()` returns `False` (`controllers/upload.py`).

Bodies that are not files are safe for another reason: JSON-RPC and XML-RPC read the body through `httprequest.get_data()` (`http.py:2012`, `addons/rpc/controllers/xmlrpc.py:132`), and werkzeug caches it by default (`cache=True`), so the next attempt gets the same bytes. Code that reads `request.httprequest.stream` directly would find it empty on the second attempt.

### What still repeats with a seekable upload

The controller runs again from the top, so it reads the file again: fine. Whatever it did with the file outside the transaction runs again too: forwarding it to an OCR or storage API, writing it to a disk path of its own, emailing it. `ir.attachment` itself is safe (note 04: content-addressed, garbage-collected).

### Demo

Upload page, opened from the *Non-seekable upload* scenario ([evidence](evidence/run_scenarios.txt)):

| Mode | First attempt | Result |
|---|---|---|
| Seekable | read 1300 bytes, 40001 | rewound; attempt 2 read 1300 bytes again and committed: HTTP 303 |
| Non-seekable | read 1300 bytes, 40001 | `RuntimeError: Cannot retry request on input file 'upload' after serialization failure`: HTTP 500, no retry |
| Non-seekable, unique violation | 23505 | the same `RuntimeError` instead of the `ValidationError`: HTTP 500 |

Core has its own test of the seekable case: `odoo/addons/test_http/tests/test_misc.py` `test_misc6_upload_file_retry`.

### Open questions

- None of our modules is known to wrap upload streams; worth a grep for `stream_factory` and `form_data_parser_class` when auditing a module.
