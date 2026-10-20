"""gzip for the text this app sends, and only the text.

`/api/connections` was 2 MB of JSON and the JS bundle 750 KB, sent as they were.
These tests pin what is compressed, what is not, and that the real app has the
middleware. A spreadsheet is a zip file already, an event stream must not be
buffered, and a small answer is not worth the bytes.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from queryhub.web import compression

BIG = {"rows": ["x" * 40] * 600}          # about 25 KB of JSON


def _app(csv_file=None):
    app = FastAPI()
    app.add_middleware(compression.TextOnlyGZipMiddleware,
                       minimum_size=1024, compresslevel=5)

    # The real app streams every response through BaseHTTPMiddleware layers, and
    # that is the shape in which a Range answer used to be packed. Keep one here.
    @app.middleware("http")
    async def _passthrough(request, call_next):
        return await call_next(request)

    if csv_file is not None:
        from fastapi.responses import FileResponse

        @app.get("/file")
        def plain_file():
            return FileResponse(csv_file, media_type="text/csv")

        @app.get("/download")
        def download():
            return FileResponse(csv_file, media_type="text/csv", filename="result.csv")

    @app.get("/json")
    def big_json():
        return JSONResponse(BIG)

    @app.get("/small")
    def small():
        return JSONResponse({"ok": True})

    @app.get("/xlsx")
    def xlsx():
        return Response(b"PK" + b"\x00" * 20000,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/png")
    def png():
        return Response(b"\x89PNG" + b"\x00" * 20000, media_type="image/png")

    @app.get("/csv")
    def csv():
        return Response("a,b\n" * 5000, media_type="text/csv")

    @app.get("/chunks")
    def chunks():
        # Text sent in many pieces, as a streamed answer is.
        return StreamingResponse((f"line {i}\n".encode() * 20 for i in range(300)),
                                 media_type="text/plain")

    @app.get("/already")
    def already():
        return Response(b"x" * 5000, media_type="text/plain",
                        headers={"Content-Encoding": "br"})

    @app.get("/stream")
    def stream():
        return StreamingResponse(iter([b"data: 1\n\n"] * 400), media_type="text/event-stream")

    return TestClient(app)


GZIP = {"Accept-Encoding": "gzip"}


def test_a_large_json_answer_is_compressed_and_small_in_transit():
    r = _app().get("/json", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.json() == BIG                      # the client sees the same data
    raw = len(JSONResponse(BIG).body)
    assert int(r.headers["content-length"]) < raw / 5


def test_a_client_that_does_not_accept_gzip_gets_the_plain_body():
    r = _app().get("/json", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert r.json() == BIG


def test_a_small_answer_is_left_alone():
    r = _app().get("/small", headers=GZIP)
    assert "content-encoding" not in r.headers


def test_csv_text_is_compressed():
    r = _app().get("/csv", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"
    assert r.text.startswith("a,b")


def test_types_that_are_compressed_already_are_not_packed_again():
    for path in ("/xlsx", "/png"):
        r = _app().get(path, headers=GZIP)
        assert "content-encoding" not in r.headers, path
        assert len(r.content) >= 20000, path


def test_an_event_stream_is_never_compressed():
    r = _app().get("/stream", headers=GZIP)
    assert "content-encoding" not in r.headers
    assert r.content.startswith(b"data: 1")


def test_the_compressible_list_is_text_only():
    for ok in ("text/html; charset=utf-8", "application/json", "text/javascript",
               "image/svg+xml", "text/csv", "TEXT/CSS"):
        assert compression.is_compressible(ok), ok
    for no in ("", "image/png", "font/woff2", "application/zip",
               "application/octet-stream", "application/pdf"):
        assert not compression.is_compressible(no), no


def test_the_real_app_installs_the_middleware_outermost():
    from queryhub.web import app as web_app
    app = web_app.create_app()
    classes = [m.cls for m in app.user_middleware]
    assert compression.TextOnlyGZipMiddleware in classes
    # user_middleware is outermost first: the packer wraps the others, so it
    # compresses the response after the security headers were added.
    assert classes[0] is compression.TextOnlyGZipMiddleware


def test_a_response_through_the_real_stack_still_decodes():
    """The app's own middlewares stream every response in chunks, and Starlette
    packs a streamed response whatever its size, so even a small error answer is
    gzip here. What matters is that the client reads it back unchanged, and that
    the security headers and the request id survive the wrapper."""
    from queryhub.web import app as web_app
    client = TestClient(web_app.create_app())
    r = client.get("/api/me", headers=GZIP)      # not signed in: a small JSON error
    assert r.status_code in (401, 403)
    assert "error" in r.json()                   # decoded, intact
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.headers.get("x-request-id")
    assert r.headers.get("x-frame-options") == "DENY"


def _csv(tmp_path):
    p = tmp_path / "result.csv"
    p.write_text("a,b\n" * 20000)               # 80 KB of text
    return p


def test_a_range_request_gets_the_plain_partial_file(tmp_path):
    """The defect: a 206 answer was gzip, while Content-Range counts plain bytes,
    so a resumed download came back corrupt."""
    client = _app(_csv(tmp_path))
    # The range is well above the 1024-byte minimum: a small one would pass
    # unpacked for that reason alone and prove nothing.
    r = client.get("/file", headers={**GZIP, "Range": "bytes=100-9999"})
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert r.headers["content-range"].startswith("bytes 100-9999/")
    assert r.content == ("a,b\n" * 20000).encode()[100:10000]


def test_a_conditional_range_request_is_also_left_plain(tmp_path):
    client = _app(_csv(tmp_path))
    etag = client.get("/file", headers={"Accept-Encoding": "identity"}).headers["etag"]
    r = client.get("/file", headers={**GZIP, "Range": "bytes=0-4999", "If-Range": etag})
    assert r.status_code == 206
    assert "content-encoding" not in r.headers
    assert r.content == ("a,b\n" * 20000).encode()[:5000]


def test_a_download_keeps_its_length_and_is_not_packed(tmp_path):
    client = _app(_csv(tmp_path))
    r = client.get("/download", headers=GZIP)
    assert "content-encoding" not in r.headers
    assert int(r.headers["content-length"]) == 80000
    assert "attachment" in r.headers["content-disposition"]


def test_the_same_file_served_inline_as_text_is_still_packed(tmp_path):
    client = _app(_csv(tmp_path))
    r = client.get("/file", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == ("a,b\n" * 20000).encode()


def test_text_sent_in_many_pieces_is_packed_and_reads_back_whole():
    r = _app().get("/chunks", headers=GZIP)
    assert r.headers["content-encoding"] == "gzip"
    assert "content-length" not in r.headers          # the length was not known
    assert r.content == b"".join(f"line {i}\n".encode() * 20 for i in range(300))


def test_a_response_that_already_has_an_encoding_is_not_touched():
    r = _app().get("/already", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "br"


def test_a_small_text_answer_still_tells_caches_that_it_varies():
    """Not packed, but another client may get it packed, so a cache must key on it."""
    r = _app().get("/small", headers=GZIP)
    assert "content-encoding" not in r.headers
    assert "accept-encoding" in r.headers.get("vary", "").lower()


def test_the_packed_length_header_matches_the_body_sent():
    """Read the bytes as they cross the wire, before the client decodes them."""
    import gzip
    with _app().stream("GET", "/json", headers=GZIP) as r:
        raw = b"".join(r.iter_raw())
    assert len(raw) == int(r.headers["content-length"])
    assert gzip.decompress(raw) == JSONResponse(BIG).body
