"""The body-size limit: an oversized body gets 413 before any route reads it."""
import asyncio
import json

from queryhub.web import body_limit


async def _echo_app(scope, receive, send):
    """Read the whole body, then answer 200 with its size."""
    total = 0
    while True:
        msg = await receive()
        total += len(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _run(app, headers, chunks):
    sent = []
    queue = list(chunks)

    async def receive():
        body = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": headers}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], body


def test_a_normal_body_passes():
    app = body_limit.BodyLimitMiddleware(_echo_app, max_bytes=100)
    status, body = _run(app, [(b"content-length", b"10")], [b"x" * 10])
    assert status == 200
    assert body == b"10"


def test_a_large_content_length_gets_413_without_reading():
    app = body_limit.BodyLimitMiddleware(_echo_app, max_bytes=100)
    status, body = _run(app, [(b"content-length", b"101")], [b"x" * 101])
    assert status == 413
    assert json.loads(body)["error"]["code"] == "payload_too_large"


def test_a_chunked_body_over_the_limit_gets_413():
    app = body_limit.BodyLimitMiddleware(_echo_app, max_bytes=100)
    status, body = _run(app, [], [b"x" * 60, b"x" * 60])
    assert status == 413
    assert json.loads(body)["error"]["code"] == "payload_too_large"


def test_a_chunked_body_under_the_limit_passes():
    app = body_limit.BodyLimitMiddleware(_echo_app, max_bytes=100)
    status, body = _run(app, [], [b"x" * 40, b"x" * 40])
    assert status == 200
    assert body == b"80"


def test_a_route_that_turns_the_read_error_into_a_400_still_gets_413():
    async def swallowing_app(scope, receive, send):
        try:
            while (await receive()).get("more_body"):
                pass
        except Exception:
            await send({"type": "http.response.start", "status": 400,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"bad"})

    app = body_limit.BodyLimitMiddleware(swallowing_app, max_bytes=100)
    status, _ = _run(app, [], [b"x" * 60, b"x" * 60])
    assert status == 413


def test_the_default_limit_is_well_above_the_largest_sql_text():
    # 200,000 characters at 4 bytes each.
    assert body_limit.MAX_BODY_BYTES > 200_000 * 4 * 4
