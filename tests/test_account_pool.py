"""Account-pool integration tests using only synthetic local HTTP services."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import struct
import threading
import time
import unittest
from urllib.parse import parse_qsl, urlsplit

import account_pool


API_KEY = "synthetic-key-for-local-tests"
CHAT_PATH = "/v1/chat/completions"
PAYLOAD = b'{"model":"synthetic-model","messages":[{"role":"user","content":"hello"}]}'


def read_body(handler):
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        parts = []
        while True:
            line = handler.rfile.readline()
            size = int(line.strip().split(b";", 1)[0], 16)
            if size == 0:
                while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(parts)
            parts.append(handler.rfile.read(size))
            if handler.rfile.read(2) != b"\r\n":
                raise ValueError("Malformed synthetic request chunk")
    return handler.rfile.read(int(handler.headers.get("Content-Length", "0")))


class LocalBackend:
    """Threaded loopback backend with observable entry and completion events."""

    def __init__(self, account_id, behavior=None):
        self.account_id = account_id
        self.behavior = behavior or self.reply_success
        self.lock = threading.Lock()
        self.requests = []
        self.inflight = 0
        self.max_inflight = 0
        self.entered = threading.Event()
        self.completed = threading.Event()
        self.release = threading.Event()
        backend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = read_body(self)
                with backend.lock:
                    backend.requests.append({
                        "path": self.path,
                        "body": body,
                        "headers": dict(self.headers),
                    })
                    backend.inflight += 1
                    backend.max_inflight = max(backend.max_inflight, backend.inflight)
                backend.entered.set()
                try:
                    backend.behavior(self, body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    with backend.lock:
                        backend.inflight -= 1
                    backend.completed.set()

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    @property
    def count(self):
        with self.lock:
            return len(self.requests)

    def reply(self, handler, status, body, content_type="application/json"):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
        handler.close_connection = True

    def reply_success(self, handler, _body):
        self.reply(handler, 200, json.dumps({"account": self.account_id}).encode())

    def hold_then_reply(self, handler, body):
        if not self.release.wait(5):
            self.reply(handler, 500, b'{"error":"synthetic release timed out"}')
            return
        self.reply_success(handler, body)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class AccountPoolTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def backend(self, account_id, behavior=None):
        backend = LocalBackend(account_id, behavior)
        self.resources.callback(backend.close)
        return backend

    def gateway(self, backends, **settings):
        pool = {
            "backends": [{"id": item.account_id, "url": item.url} for item in backends],
            "max_concurrent_per_account": 1,
            "queue_timeout_sec": 0.15,
            "failure_cooldown_sec": 0.5,
            "request_timeout_sec": 3,
        }
        pool.update(settings)
        server = account_pool.make_server(
            {"host": "127.0.0.1", "port": 0, "api_keys": [API_KEY]}, pool
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        def close():
            # Release held test backends before shutting down the gateway.
            for backend in backends:
                backend.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.resources.callback(close)
        return server.server_address

    def request(self, address, path=CHAT_PATH, body=PAYLOAD, key=API_KEY,
                method="POST", chunked=False):
        connection = http.client.HTTPConnection(*address, timeout=4)
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        try:
            connection.request(method, path, body=body, headers=headers,
                               encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_sequential_requests_rotate_evenly(self):
        first, second = self.backend("u1"), self.backend("u2")
        address = self.gateway([first, second])
        accounts = []
        for _ in range(6):
            status, body, _headers = self.request(address)
            self.assertEqual(status, 200)
            accounts.append(json.loads(body)["account"])
        self.assertEqual(accounts, ["u1", "u2"] * 3)
        self.assertEqual((first.count, second.count), (3, 3))

    def test_parallel_requests_use_different_accounts_and_limit_each_to_one(self):
        first, second = self.backend("u1"), self.backend("u2")
        first.behavior = first.hold_then_reply
        second.behavior = second.hold_then_reply
        address = self.gateway([first, second])
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(self.request, address) for _ in range(2)]
            try:
                self.assertTrue(first.entered.wait(2), "First account never received a request")
                self.assertTrue(second.entered.wait(2), "Second account never received a request")
                self.assertEqual((first.inflight, second.inflight), (1, 1))
            finally:
                first.release.set()
                second.release.set()
            self.assertEqual({json.loads(item.result(timeout=3)[1])["account"]
                              for item in requests}, {"u1", "u2"})
        self.assertEqual((first.max_inflight, second.max_inflight), (1, 1))

    def test_authentication_rejects_missing_and_invalid_keys_without_backend_calls(self):
        for settings in ({}, {"require_api_key": True}):
            with self.subTest(settings=settings):
                backend = self.backend("u1")
                address = self.gateway([backend], **settings)
                for key in (None, "none", "wrong-synthetic-key"):
                    with self.subTest(key=key):
                        status, _body, _headers = self.request(address, key=key)
                        self.assertEqual(status, 401)
                self.assertEqual(backend.count, 0)
                self.assertEqual(self.request(address, key=API_KEY)[0], 200)
                self.assertEqual(backend.count, 1)

    def test_optional_auth_accepts_placeholders_and_missing_key(self):
        backend = self.backend("u1")
        address = self.gateway([backend], require_api_key=False)
        for key in ("none", "arbitrary-synthetic-placeholder", None):
            with self.subTest(key=key):
                status, body, _headers = self.request(address, key=key)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["account"], "u1")
        self.assertEqual(backend.count, 3)
        for forwarded in backend.requests:
            headers = {key.lower(): value for key, value in forwarded["headers"].items()}
            self.assertEqual(headers["authorization"], "Bearer " + API_KEY)
            self.assertNotIn("x-api-key", headers)
            self.assertNotIn("x-goog-api-key", headers)

    def test_optional_auth_discards_all_client_credentials_before_forwarding(self):
        backend = self.backend("u1")
        address = self.gateway([backend], require_api_key=False)
        connection = http.client.HTTPConnection(*address, timeout=4)
        try:
            connection.request(
                "GET", "/v1/models?key=client-query-key&alt=sse&key=another-query-key&flag=",
                headers={
                    "Authorization": "Bearer client-bearer-placeholder",
                    "x-api-key": "client-api-header-placeholder",
                    "x-goog-api-key": "client-google-header-placeholder",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["account"], "u1")
        finally:
            connection.close()
        self.assertEqual(backend.count, 1)
        forwarded = backend.requests[0]
        target = urlsplit(forwarded["path"])
        self.assertEqual(target.path, "/v1/models")
        self.assertEqual(parse_qsl(target.query, keep_blank_values=True), [
            ("alt", "sse"), ("flag", ""),
        ])
        headers = {key.lower(): value for key, value in forwarded["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer " + API_KEY)
        self.assertNotIn("x-api-key", headers)
        self.assertNotIn("x-goog-api-key", headers)
        for client_credential in (
            "client-query-key", "another-query-key", "client-bearer-placeholder",
            "client-api-header-placeholder", "client-google-header-placeholder",
        ):
            self.assertNotIn(client_credential, str(forwarded))

    def test_optional_auth_setting_requires_a_boolean(self):
        for value in (None, 0, 1, 0.0, 1.0, "false", "true", "", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    account_pool.AccountPool({
                        "backends": [{"id": "u1", "url": "http://127.0.0.1:18082"}],
                        "require_api_key": value,
                    }, 18081)

    def test_optional_auth_still_requires_internal_worker_keys(self):
        pool = {
            "backends": [{"id": "u1", "url": "http://127.0.0.1:18082"}],
            "require_api_key": False,
        }
        for keys in (None, [], [""], [None], "none"):
            with self.subTest(keys=keys):
                with self.assertRaises(ValueError):
                    account_pool.make_server({
                        "host": "127.0.0.1", "port": 0, "api_keys": keys,
                    }, pool)

    def test_query_key_auth_is_removed_and_canonicalized_before_forwarding(self):
        backend = self.backend("u1")
        address = self.gateway([backend])
        status, _body, _headers = self.request(
            address, path="/v1/models?key=wrong-synthetic-key&alt=sse",
            method="GET", body=None, key=None,
        )
        self.assertEqual(status, 401)
        self.assertEqual(backend.count, 0)

        connection = http.client.HTTPConnection(*address, timeout=4)
        try:
            connection.request(
                "GET", "/v1/models?key=" + API_KEY + "&alt=sse&flag=&tag=one&tag=two",
                headers={
                    "Authorization": "Bearer discarded-synthetic-key",
                    "x-api-key": "discarded-header-key",
                    "x-goog-api-key": "discarded-google-key",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["account"], "u1")
        finally:
            connection.close()
        self.assertEqual(backend.count, 1)
        forwarded = backend.requests[0]
        target = urlsplit(forwarded["path"])
        self.assertEqual(target.path, "/v1/models")
        self.assertEqual(parse_qsl(target.query, keep_blank_values=True), [
            ("alt", "sse"), ("flag", ""), ("tag", "one"), ("tag", "two"),
        ])
        self.assertNotIn(API_KEY, forwarded["path"])
        headers = {key.lower(): value for key, value in forwarded["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer " + API_KEY)
        self.assertNotIn("x-api-key", headers)
        self.assertNotIn("x-goog-api-key", headers)

    def test_retryable_errors_fail_over_and_put_failed_account_in_cooldown(self):
        for upstream_status in (429, 502, 503, 504):
            with self.subTest(upstream_status=upstream_status):
                first, second = self.backend("u1"), self.backend("u2")

                def failure(handler, _body, status=upstream_status, backend=first):
                    backend.reply(handler, status, b'{"error":"synthetic upstream failure"}')

                first.behavior = failure
                address = self.gateway([first, second], failure_cooldown_sec=2)
                for _ in range(2):
                    status, body, _headers = self.request(address)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["account"], "u2")
                self.assertEqual(first.count, 1, "Failed account was reused during cooldown")
                self.assertEqual(second.count, 2)

    def test_client_error_is_returned_without_retry_on_another_account(self):
        first, second = self.backend("u1"), self.backend("u2")
        expected = b'{"error":"synthetic invalid model"}'
        first.behavior = lambda handler, _body: first.reply(handler, 400, expected)
        address = self.gateway([first, second])
        status, body, _headers = self.request(address)
        self.assertEqual((status, body), (400, expected))
        self.assertEqual((first.count, second.count), (1, 0))

    def test_all_busy_accounts_return_queue_timeout_without_oversubscription(self):
        first, second = self.backend("u1"), self.backend("u2")
        first.behavior = first.hold_then_reply
        second.behavior = second.hold_then_reply
        address = self.gateway([first, second])
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(self.request, address) for _ in range(2)]
            try:
                self.assertTrue(first.entered.wait(2))
                self.assertTrue(second.entered.wait(2))
                status, body, _headers = self.request(address)
                self.assertEqual(status, 503)
                self.assertIn("pool_busy", body.decode())
                self.assertEqual((first.count, second.count), (1, 1))
            finally:
                first.release.set()
                second.release.set()
            for request in requests:
                self.assertEqual(request.result(timeout=3)[0], 200)
        self.assertEqual((first.max_inflight, second.max_inflight), (1, 1))

    def test_queued_request_is_served_when_an_account_becomes_free(self):
        backend = self.backend("u1")
        backend.behavior = backend.hold_then_reply
        address = self.gateway([backend], queue_timeout_sec=2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.request, address)
            self.assertTrue(backend.entered.wait(2))
            second = executor.submit(self.request, address)
            backend.release.set()
            self.assertEqual(first.result(timeout=3)[0], 200)
            self.assertEqual(second.result(timeout=3)[0], 200)
        self.assertEqual(backend.count, 2)
        self.assertEqual(backend.max_inflight, 1)

    def test_chunked_client_request_reaches_backend_as_original_body(self):
        backend = self.backend("u1")

        def echo(handler, body):
            backend.reply(handler, 200, body)

        backend.behavior = echo
        address = self.gateway([backend])
        chunks = iter([PAYLOAD[:13], PAYLOAD[13:41], PAYLOAD[41:]])
        status, body, _headers = self.request(address, body=chunks, chunked=True)
        self.assertEqual((status, body), (200, PAYLOAD))
        self.assertEqual(backend.requests[0]["body"], PAYLOAD)

    def test_generation_routes_discard_client_thinking_controls_only_at_option_locations(self):
        backend = self.backend("u1")
        address = self.gateway([backend])
        controls = {
            "reasoning": {"effort": "high"}, "reasoning_effort": "high",
            "thinking": {"type": "enabled"}, "thinking_config": {"budget": 123},
            "thinkingConfig": {"thinkingBudget": 456}, "thinking_level": "high",
            "thinkingLevel": "high", "thinking_budget": 123, "thinkingBudget": 456,
            "include_thoughts": True, "includeThoughts": True,
            "enable_thinking": True, "enableThinking": True,
        }
        content = {
            "model": "synthetic-model-low",
            "messages": [{"role": "user", "content": "thinkingBudget is a field in my schema"}],
            "tools": [{"type": "function", "function": {
                "name": "example", "parameters": {"type": "object", "properties": {
                    "reasoning": {"type": "string"}, "thinkingBudget": {"type": "integer"},
                }},
            }}],
            "metadata": {"thinking": "ordinary content"},
            "temperature": 0.3,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 71},
            "generation_config": {"top_p": 0.7},
            "extra_body": {
                "other_extension": {"thinking": "ordinary extension data"},
                "generationConfig": {"topK": 4},
                "generation_config": {"max_output_tokens": 72},
            },
        }
        expected = json.loads(json.dumps(content))
        for options in (content, content["generationConfig"], content["generation_config"],
                        content["extra_body"], content["extra_body"]["generationConfig"],
                        content["extra_body"]["generation_config"]):
            options.update(controls)
        payload = json.dumps(content).encode()
        for path in (CHAT_PATH, "/v1/responses", "/v1/models/synthetic-model-low:generateContent",
                     "/v1beta/models/synthetic-model-low:generateContent",
                     "/v1/models/synthetic-model-low:streamGenerateContent?alt=sse",
                     "/v1beta/models/synthetic-model-low:streamGenerateContent?alt=sse"):
            with self.subTest(path=path):
                status, _body, _headers = self.request(address, path=path, body=payload)
                self.assertEqual(status, 200)
                forwarded = backend.requests[-1]
                self.assertEqual(json.loads(forwarded["body"]), expected)
                self.assertEqual(int(forwarded["headers"]["Content-Length"]),
                                 len(forwarded["body"]))

    def test_thinking_sanitizer_preserves_unchanged_or_invalid_bodies_and_other_routes(self):
        backend = self.backend("u1")
        address = self.gateway([backend])
        for payload in (b' { "model" : "synthetic-model-low", "messages" : [] }\n',
                        b'{"thinking":', b'[ {"thinking":true} ]', b'null', b'123',
                        b'"thinking"', b'\xff', b'{"extra_body":null,"generationConfig":[]}',
                        b'{"messages":[{"thinking":"keep"}],"tools":[{"reasoning":"keep"}]}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(address, body=payload)[0], 200)
                self.assertEqual(backend.requests[-1]["body"], payload)
        payload = b' { "thinking": { "type" : "enabled" } }\n'
        for path in ("/v1/models", "/v1/models/synthetic:countTokens",
                     "/v1/responses/previous-id", "/v1beta/models/synthetic:other"):
            with self.subTest(path=path):
                self.assertEqual(self.request(address, path=path, body=payload)[0], 200)
                self.assertEqual(backend.requests[-1]["body"], payload)

    def test_failover_reuses_the_same_sanitized_generation_body(self):
        first, second = self.backend("u1"), self.backend("u2")
        first.behavior = lambda handler, _body: first.reply(handler, 503, b'{"error":"retry"}')
        address = self.gateway([first, second])
        payload = b'{"model":"synthetic-low","reasoning_effort":"high","messages":[]}'
        self.assertEqual(self.request(address, body=payload)[0], 200)
        self.assertEqual((first.count, second.count), (1, 1))
        self.assertEqual(first.requests[0]["body"], second.requests[0]["body"])
        self.assertEqual(json.loads(second.requests[0]["body"]),
                         {"model": "synthetic-low", "messages": []})

    def test_sanitized_stream_request_still_relays_all_events(self):
        backend = self.backend("u1")
        events = b'data: {"piece":"hello"}\n\ndata: [DONE]\n\n'
        backend.behavior = lambda handler, _body: backend.reply(
            handler, 200, events, "text/event-stream",
        )
        address = self.gateway([backend])
        payload = b'{"model":"synthetic-low","thinking":{"type":"enabled"},"stream":true}'
        status, body, headers = self.request(address, path="/v1/responses", body=payload)
        self.assertEqual((status, body), (200, events))
        self.assertEqual({key.lower(): value for key, value in headers.items()}["content-type"],
                         "text/event-stream")
        self.assertEqual(json.loads(backend.requests[0]["body"]),
                         {"model": "synthetic-low", "stream": True})

    def test_stream_first_event_is_forwarded_before_upstream_finishes(self):
        backend = self.backend("u1")
        first_event = b'data: {"piece":"first"}\n\n'
        last_event = b'data: [DONE]\n\n'

        def stream(handler, _body):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for data in (first_event, last_event):
                handler.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
                handler.wfile.flush()
                if data == first_event and not backend.release.wait(5):
                    return
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()

        backend.behavior = stream
        address = self.gateway([backend])
        connection = http.client.HTTPConnection(*address, timeout=3)
        self.resources.callback(connection.close)
        connection.request("POST", CHAT_PATH, body=PAYLOAD,
                           headers={"Authorization": "Bearer " + API_KEY})
        with ThreadPoolExecutor(max_workers=1) as executor:
            def receive_first():
                response = connection.getresponse()
                return response, response.read(len(first_event))

            pending = executor.submit(receive_first)
            try:
                response, received = pending.result(timeout=1.5)
                self.assertEqual(response.status, 200)
                self.assertEqual(received, first_event)
                self.assertFalse(backend.completed.is_set(), "Upstream finished before release")
            finally:
                backend.release.set()
            self.assertEqual(response.read(), last_event)

    def test_truncated_stream_does_not_retry_after_response_is_committed(self):
        first, second = self.backend("u1"), self.backend("u2")
        first_event = b'data: {"piece":"first"}\n\n'

        def truncated(handler, _body):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(first_event) + 100))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(first_event)
            handler.wfile.flush()
            handler.close_connection = True

        first.behavior = truncated
        address = self.gateway([first, second])
        connection = http.client.HTTPConnection(*address, timeout=3)
        try:
            connection.request("POST", CHAT_PATH, body=PAYLOAD,
                               headers={"Authorization": "Bearer " + API_KEY})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            try:
                received = response.read()
            except http.client.IncompleteRead as error:
                received = error.partial
            self.assertEqual(received, first_event)
        finally:
            connection.close()
        self.assertTrue(first.completed.wait(2))
        self.assertEqual((first.count, second.count), (1, 0))

    def test_client_disconnect_during_stream_does_not_reroute_request(self):
        first, second = self.backend("u1"), self.backend("u2")
        first_event = b"data: first\n\n"

        def stream(handler, _body):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(first_event)
            handler.wfile.flush()
            if first.release.wait(5):
                for _ in range(256):
                    handler.wfile.write(b"data: " + b"x" * 4096 + b"\n\n")
                    handler.wfile.flush()
            handler.close_connection = True

        first.behavior = stream
        address = self.gateway([first, second])
        client_socket = socket.create_connection(address, timeout=3)
        try:
            request_head = (
                "POST %s HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer %s\r\n"
                "Content-Length: %d\r\n\r\n" % (CHAT_PATH, API_KEY, len(PAYLOAD))
            ).encode()
            client_socket.sendall(request_head + PAYLOAD)
            response = http.client.HTTPResponse(client_socket)
            response.begin()
            self.assertEqual(response.read(len(first_event)), first_event)
            # A reset makes the disconnect observable without waiting on socket buffers.
            linger_format = "hh" if os.name == "nt" else "ii"
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                     struct.pack(linger_format, 1, 0))
            response.close()
        finally:
            client_socket.close()
            first.release.set()
        self.assertTrue(first.completed.wait(3), "Upstream stream did not terminate")
        self.assertEqual((first.count, second.count), (1, 0))

    def test_status_requires_auth_and_reports_only_safe_account_stats(self):
        first, second = self.backend("u1"), self.backend("u2")
        address = self.gateway([first, second])
        status, _body, _headers = self.request(address, path="/pool/status", method="GET",
                                              body=None, key=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.request(address)[0], 200)
        # Receiving a full Content-Length body can precede the gateway handler's
        # finally block.  Observe its released slot before checking final counters.
        deadline = time.monotonic() + 2
        while True:
            status, body, _headers = self.request(address, path="/pool/status", method="GET",
                                                 body=None)
            self.assertEqual(status, 200)
            report = json.loads(body)
            if all(item["inflight"] == 0 for item in report["backends"]):
                break
            if time.monotonic() >= deadline:
                self.fail("Completed request did not release its account slot")
            threading.Event().wait(0.01)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["mode"], "account_pool")
        self.assertEqual(report["max_concurrent_per_account"], 1)
        accounts = {item["id"]: item for item in report["backends"]}
        self.assertEqual(set(accounts), {"u1", "u2"})
        self.assertEqual(accounts["u1"]["served"], 1)
        self.assertEqual(accounts["u2"]["served"], 0)
        for item in accounts.values():
            self.assertEqual(item["inflight"], 0)
            self.assertEqual(item["failures"], 0)
            self.assertEqual(item["cooldown_remaining_sec"], 0)
        decoded = body.decode()
        for secret in (API_KEY, "Authorization", first.url, second.url, "cookie", "token"):
            self.assertNotIn(secret, decoded)
        self.assertEqual((first.count, second.count), (1, 0))


if __name__ == "__main__":
    unittest.main()
