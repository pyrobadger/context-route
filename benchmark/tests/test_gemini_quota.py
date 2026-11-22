from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from threading import Event
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest

from benchmark.runner.gemini_gateway import GeminiGateway, MODEL_PATH
from benchmark.runner.gemini_quota import Ledger, Limits, MODEL, PACIFIC_ZONE, QuotaBlocked, pacific_day, provider_pause
from benchmark.runner import run as runner
from benchmark.runner.run import GEMINI_TITLE_MODEL, gemini_request_accounting, main

PACIFIC = ZoneInfo(PACIFIC_ZONE)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 21, 18, tzinfo=PACIFIC).timestamp()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def ledger(tmp_path, limits):
    clock = Clock()
    return Ledger(tmp_path / "quota.db", limits, clock=clock, sleep=clock.advance), clock


def test_rpm_and_tpm_are_rolling_and_include_token_count_requests(tmp_path):
    log, clock = ledger(tmp_path, Limits(rpm=2, tpm=1000))
    started = clock()
    log.reserve("run", "count_tokens")
    log.reserve("run", "generate", 950, 800)
    log.reserve("run", "generate", 100, 50)
    assert clock() >= started + 60
    assert log.snapshot()["remaining_rpm"] == 1

    log, clock = ledger(tmp_path / "second", Limits(rpm=10, tpm=1000))
    started = clock()
    req = log.reserve("run", "generate", 890, 800)
    log.settle(req, 200, {"promptTokenCount": 910, "toolUsePromptTokenCount": 20, "candidatesTokenCount": 5,
                         "thoughtsTokenCount": 45, "totalTokenCount": 980})
    assert log.snapshot()["remaining_tpm"] == 70
    log.reserve("run", "generate", 90, 45)
    assert clock() >= started + 60
    assert log.metrics("run")["observed_output_tokens"] == 50
    with pytest.raises(QuotaBlocked, match="request_exceeds_token_budget"):
        log.reserve("run", "generate", 1001)


def test_daily_limit_survives_restart_and_resets_at_pacific_midnight(tmp_path):
    log, clock = ledger(tmp_path, Limits(rpd=2))
    clock.now = datetime(2026, 11, 21, 23, 59, 50, tzinfo=PACIFIC).timestamp()
    req = log.reserve("a", "count_tokens")
    log.settle(req, 500)
    log.reserve("a", "generate", 128)
    second = Ledger(log.path, log.limits, clock=clock, sleep=clock.advance)
    with pytest.raises(QuotaBlocked, match="daily_budget_exhausted"):
        second.ready()
    clock.advance(10)
    assert second.snapshot()["remaining_rpd"] == 2
    assert second.snapshot()["remaining_rpm"] == 8


def test_parallel_reservations_cannot_overshoot_daily_cap(tmp_path):
    log, _ = ledger(tmp_path, Limits(rpd=3))

    def reserve(index):
        try:
            log.reserve(str(index), "count_tokens")
            return True
        except QuotaBlocked:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(reserve, range(8))) == 3


def test_retry_after_and_daily_error_block_new_requests(tmp_path):
    log, clock = ledger(tmp_path, Limits())
    delay = {"error": {"details": [{"retryDelay": "12.5s"}]}}
    assert provider_pause(clock(), "5", delay) == clock() + 12.5
    assert provider_pause(clock(), "Fri, 29 Dec 2028 10:00:00 GMT", {}) > clock()
    daily = {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    assert provider_pause(clock(), "5", daily) == pacific_day(clock())[1]
    log.pause(provider_pause(clock(), "20", delay))
    with pytest.raises(QuotaBlocked, match="provider_429"):
        log.reserve("next", "count_tokens")
    clock.advance(20)
    log.reserve("next", "count_tokens")


class LocalResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self.payload

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def call(gateway, path, payload):
    return urlopen(Request(gateway.settings["url"] + path, json.dumps(payload).encode(),
                          {"x-contextroute-gate": gateway.settings["token"], "x-goog-api-key": "local-test-key"}), timeout=8)


def test_gateway_verifies_model_counts_full_payload_and_records_usage(tmp_path):
    log, _ = ledger(tmp_path, Limits())
    calls = []
    usage = {"promptTokenCount": 51, "candidatesTokenCount": 9, "totalTokenCount": 60}

    class LocalUpstream:
        def request(self, method, url, body, headers):
            self.url = url
            calls.append((method, url, json.loads(body)))
            assert headers["x-goog-api-key"] == "local-test-key"

        def getresponse(self):
            return LocalResponse(200, {"totalTokens": 51} if self.url.endswith(":countTokens") else
                                 {"usageMetadata": usage, "modelVersion": MODEL})

        def close(self):
            pass

    payload = {"contents": [{"parts": [{"text": "test"}]}],
               "systemInstruction": {"parts": [{"text": "system context"}]},
               "tools": [{"functionDeclarations": [{"name": "read"}]}]}
    with GeminiGateway(log, "test-run", connection=LocalUpstream) as gate:
        with pytest.raises(HTTPError) as failure:
            call(gate, "/v1beta/models/other:generateContent", payload)
        assert failure.value.code == 403
        assert not calls
        assert json.load(call(gate, MODEL_PATH + ":generateContent", payload))["modelVersion"] == MODEL
    assert calls[0] == ("POST", MODEL_PATH + ":countTokens", {"generateContentRequest": {**payload, "model": "models/" + MODEL}})
    assert calls[1] == ("POST", MODEL_PATH + ":generateContent", payload)
    quota = log.metrics("test-run")
    assert quota["api_requests"] == 2
    assert quota["generation_requests"] == 1
    assert quota["observed_total_tokens"] == 60
    assert quota["response_model_versions"] == [MODEL]


def test_google_429_is_recorded_and_prevents_further_upstream_traffic(tmp_path):
    log, _ = ledger(tmp_path, Limits())
    sent = []

    class Upstream:
        def request(self, method, url, body, headers):
            sent.append(url)

        def getresponse(self):
            return LocalResponse(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, {"Retry-After": "120"})

        def close(self):
            pass

    with GeminiGateway(log, "blocked", connection=Upstream) as gate:
        for _ in range(2):
            with pytest.raises(HTTPError) as failure:
                call(gate, MODEL_PATH + ":generateContent", {"contents": [{"parts": [{"text": "test"}]}]})
            assert failure.value.code == 429
            assert failure.value.headers["Retry-After"] == "120"
    assert sent == [MODEL_PATH + ":countTokens"]
    assert log.metrics("blocked")["provider_429_count"] == 1


def test_cli_never_mixes_gemini_into_original_output():
    args = ["--models", "google/" + MODEL, "--contexts", "C1", "--output", "benchmark/results/grid", "--dry-run"]
    with pytest.raises(SystemExit, match="requires --gemini-quota"):
        main(args)
    with pytest.raises(SystemExit, match="requires a separate output"):
        main([*args, "--gemini-quota"])
    for config in ({"rpm": 11}, {"tpm": 200_001}, {"rpd": 401}):
        with pytest.raises(ValueError):
            Limits(**config)


def test_gemini_accounting_rejects_title_alias_and_missing_metered_requests():
    events = [{"type": "auxiliary_request", "request_kind": "title", "actual_model": GEMINI_TITLE_MODEL}]
    tokens = {"input_tokens": 100, "output_tokens": 12, "total_tokens": 112}
    quota = {"generation_requests": 2, "token_count_requests": 2, "api_requests": 4,
             "generation_requests_with_usage": 2, "upstream_statuses": [200],
             "response_model_versions": [MODEL], **{"observed_" + k: v for k, v in tokens.items()}}
    assert gemini_request_accounting(events, quota, 1, tokens)["verified"]
    alias = [{**events[0], "actual_model": "google/gemini-flash-lite-latest#minimal"}]
    assert not gemini_request_accounting(alias, quota, 1, tokens)["verified"]
    assert not gemini_request_accounting(events, {**quota, "api_requests": 3}, 1, tokens)["verified"]
    assert not gemini_request_accounting(events, quota, 1, {**tokens, "total_tokens": 113})["verified"]


def test_native_opencode_pins_and_meters_gemini_title_and_primary(tmp_path, monkeypatch):
    if not (runner.ROOT / "dist/plugin/index.js").is_file():
        pytest.skip("Build ContextRoute to run the native OpenCode integration test.")
    try:
        executable = runner.opencode_executable()
    except RuntimeError as error:
        pytest.skip(str(error))
    version = runner.run_process([executable, "--version"], tmp_path).stdout.strip()
    if version != "opencode v2.2.7":
        pytest.skip("This integration test uses OpenCode 2.2.7.")

    class StreamResponse(LocalResponse):
        def __init__(self):
            super().__init__(200, {
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Loopback response"}]},
                                "index": 0, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 51, "candidatesTokenCount": 9, "totalTokenCount": 60},
                "modelVersion": MODEL,
            }, {"Content-Type": "text/event-stream"})
            self.lines = iter([b"data: " + self.payload + b"\n", b"\n"])

        def readline(self):
            return next(self.lines, b"")

    wire = []
    title_arrived = Event()

    class Upstream:
        def request(self, method, url, body, headers):
            if headers.get("x-goog-api-key") != "loopback-title-test":
                raise ValueError("The isolated loopback credential was not used.")
            self.path = url
            self.payload = json.loads(body)
            wire.append((url, self.payload))

        def getresponse(self):
            if self.path.endswith(":countTokens"):
                return LocalResponse(200, {"totalTokens": 51})
            if "<contextroute_repository>" in json.dumps(self.payload):
                assert title_arrived.wait(10), "The primary request did not see a separate title request."
            else:
                title_arrived.set()
            return StreamResponse()

        def close(self):
            pass

    options = runner.gemini_project_settings
    def local_catalog(settings):
        project = options(settings)
        google = project["providers"]["google"]
        google["settings"]["apiKey"] = "loopback-title-test"
        google["package"] = "@opencode/ai/providers/google"
        google["models"] = {
            model: {"name": model, "limit": {"context": 50000, "output": 4096},
                    "variants": [{"id": "minimal", "settings": {"thinkingConfig": {"thinkingLevel": "minimal"}}}]}
            for model in (MODEL, "gemini-flash-lite-latest")
        }
        return project

    for location in ("CONFIG", "DATA", "CACHE", "STATE"):
        monkeypatch.setenv("XDG_" + location + "_HOME", str(tmp_path / location.lower()))
    monkeypatch.setenv("GEMINI_API_KEY", "loopback-title-test")
    monkeypatch.setattr(runner, "gemini_project_settings", local_catalog)
    monkeypatch.setattr(runner, "GeminiGateway", lambda log, attempt: GeminiGateway(log, attempt, connection=Upstream))
    args = runner.parse_args(["--models", "google/" + MODEL, "--contexts", "C3", "--gemini-quota",
                              "--output", str(tmp_path / "results")])
    base = tmp_path / "fixture"
    commit = runner.fixture_repository(base)
    task = next(task for task in runner.load_json(runner.TASKS / "tasks.json") if task["id"] == "daily-report")
    config = runner.Configuration("forced", "google/" + MODEL, "google/" + MODEL, "C3")
    row, _ = runner.execute(base, commit, task, config, args, executable, "loopback-only", 1, version,
                            Ledger(tmp_path / "quota.db", Limits()))
    assert row["execution_status"] == "completed", row
    assert row["model_id"] == "google/" + MODEL
    assert row["context_wire_verified"]
    assert row["gemini_request_accounting"]["verified"]
    assert row["gemini_request_accounting"]["auxiliary"] == [{"kind": "title", "model": GEMINI_TITLE_MODEL}]
    assert row["gemini_quota"]["api_requests"] == 4
    assert row["gemini_quota"]["observed_total_tokens"] == 120
    generated = [body for url, body in wire if url == MODEL_PATH + ":streamGenerateContent?alt=sse"]
    assert len(generated) == 2
    assert sum("<contextroute_repository>" in json.dumps(body) for body in generated) == 1
    assert row["resolved"] is False  # The mock returns text and leaves the fixture's known bug intact.
