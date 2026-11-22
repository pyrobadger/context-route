"""Request-level governor for OpenCode's native Google HTTP transport."""

from http.client import HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from secrets import compare_digest, token_urlsafe
from threading import Event, Thread
from urllib.parse import urlsplit

from .gemini_quota import MODEL, QuotaBlocked, provider_pause

MODEL_PATH = "/v1beta/models/" + MODEL
ALLOWED = {MODEL_PATH + ":streamGenerateContent": "alt=sse", MODEL_PATH + ":generateContent": ""}
MAX_BODY = 8 * 1024 * 1024


def connect():
    return HTTPSConnection("generativelanguage.googleapis.com", timeout=35)


def validate_payload(value):
    if not isinstance(value, dict) or not value.get("contents"):
        raise ValueError("Missing Gemini contents")
    if value.get("cachedContent"):
        raise ValueError("Cached content is disabled in the free-tier benchmark")
    if any(value.get(key) not in (None, "standard") for key in ("serviceTier", "service_tier")):
        raise ValueError("Only standard inference is permitted")
    for tool in value.get("tools", []):
        if not isinstance(tool, dict) or set(tool) != {"functionDeclarations"}:
            raise ValueError("Only OpenCode function declarations are permitted")


class GeminiGateway:
    def __init__(self, ledger, attempt, *, connection=connect):
        self.ledger = ledger
        self.attempt = attempt
        self.connection = connection
        self.token = token_urlsafe(32)
        self.stop = Event()
        self.errors = []

    def __enter__(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    gateway.handle(self)
                except QuotaBlocked as error:
                    gateway.errors.append(error.reason)
                    self.problem(429, error.reason, error.retry_at)
                except (ValueError, TypeError):
                    gateway.errors.append("guard_rejected_request")
                    self.problem(400, "guard_rejected_request")
                except (OSError, HTTPException):
                    gateway.errors.append("transport_error")
                    if not getattr(self, "reply_started", False):
                        self.problem(502, "gemini_transport_error")

            def problem(self, status, reason, retry_at=None):
                if getattr(self, "reply_started", False):
                    return
                body = json.dumps({"error": {"code": status, "message": reason,
                                    "status": "RESOURCE_EXHAUSTED" if status == 429 else "FAILED_PRECONDITION"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if retry_at is not None:
                    self.send_header("Retry-After", str(max(1, math.ceil(retry_at-gateway.ledger.clock()))))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    @property
    def settings(self):
        return {"url": f"http://127.0.0.1:{self.server.server_port}", "token": self.token}

    def __exit__(self, *_exc):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def handle(self, handler):
        target = urlsplit(handler.path)
        if (not compare_digest(handler.headers.get("x-contextroute-gate", ""), self.token)
            or target.path not in ALLOWED or ALLOWED[target.path] != target.query):
            handler.problem(403, "unauthorized_gemini_gateway_request")
            return
        api_key = handler.headers.get("x-goog-api-key")
        size = int(handler.headers.get("Content-Length", "0"))
        if not api_key or not 0 < size <= MAX_BODY:
            raise ValueError("Missing access or invalid payload size")
        raw = handler.rfile.read(size)
        data = json.loads(raw)
        validate_payload(data)

        count_body = json.dumps({"generateContentRequest": {**data, "model": "models/" + MODEL}}).encode()
        request_id = self.ledger.reserve(self.attempt, "count_tokens", cancelled=self.stop.is_set)
        conn = self.connection()
        count_status = None
        try:
            conn.request("POST", MODEL_PATH + ":countTokens", count_body, self.headers(api_key))
            response = conn.getresponse()
            count_status = response.status
            payload = response.read()
            if not self.check_status(handler, response, payload):
                return
            estimate = json.loads(payload).get("totalTokens")
            if type(estimate) is not int or estimate < 0:
                raise ValueError("Missing provider input token count")
        finally:
            self.ledger.settle(request_id, count_status)
            conn.close()

        # Reserve the provider count plus headroom for small accounting differences.
        reserved = estimate + max(128, math.ceil(estimate * 0.05))
        request_id = self.ledger.reserve(self.attempt, "generate", reserved, estimate, self.stop.is_set)
        conn = self.connection()
        status = usage = model_version = None
        try:
            conn.request("POST", handler.path, raw, self.headers(api_key))
            response = conn.getresponse()
            status = response.status
            if not 200 <= status < 300:
                self.check_status(handler, response, response.read())
                return
            content_type = response.getheader("Content-Type", "application/json")
            streaming = "text/event-stream" in content_type
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.reply_started = True
            chunks = iter(response.readline, b"") if streaming else [response.read()]
            for chunk in chunks:
                encoded = chunk[5:].strip() if streaming and chunk.startswith(b"data:") else chunk if not streaming else b""
                if encoded and encoded != b"[DONE]":
                    packet = json.loads(encoded)
                    usage = packet.get("usageMetadata", usage)
                    model_version = packet.get("modelVersion", model_version)
                handler.wfile.write(chunk)
                handler.wfile.flush()
        finally:
            self.ledger.settle(request_id, status, usage, model_version)
            conn.close()

    @staticmethod
    def headers(api_key):
        return {"x-goog-api-key": api_key, "Content-Type": "application/json", "Accept-Encoding": "identity"}

    def check_status(self, handler, response, body):
        if 200 <= response.status < 300:
            return True
        if response.status == 429:
            try:
                details = json.loads(body)
            except (ValueError, TypeError):
                details = {}
            until = provider_pause(self.ledger.clock(), response.getheader("Retry-After"), details)
            self.ledger.pause(until)
            raise QuotaBlocked("provider_429", until)
        # Preserve the status without retaining the provider's error body or headers.
        name = f"google_http_{response.status}"
        self.errors.append(name)
        handler.problem(response.status if 400 <= response.status <= 599 else 502, name)
        return False

