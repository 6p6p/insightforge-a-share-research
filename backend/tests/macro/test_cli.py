"""World Bank macro CLI tests (stage 2C.1, no real DB / network)."""

import json

from app.cli import fetch_world_bank_macro as cli
from app.macro.world_bank.errors import WorldBankApiError, WorldBankProviderNotReady
from tests.macro.world_bank.helpers import sample_result

_ARGS = [
    "--country",
    "CHN",
    "--indicator",
    "SP.POP.TOTL",
    "--start-year",
    "2020",
    "--end-year",
    "2024",
]


def _install_fake_provider(monkeypatch, fetch_fn, *, test_settings):
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)

    class _FakeProvider:
        def __init__(self, sessionmaker):
            self.sessionmaker = sessionmaker

        async def fetch(self, query):
            return await fetch_fn(query)

    monkeypatch.setattr(cli, "WorldBankProvider", _FakeProvider)


def test_success_returns_0_and_json(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        return sample_result()

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["provider_key"] == "world_bank"
    assert payload["query"]["country_code"] == "CHN"
    assert payload["provider_snapshot"]["authority_tier"] == 1
    assert payload["provider_snapshot"]["acquisition_method"] == "official_api"


def test_stdout_is_pure_json(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        return sample_result()

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 0
    out = capsys.readouterr().out
    # 整段 stdout 必须能被当作单个 JSON 对象解析，无多余文本。
    parsed = json.loads(out.strip())
    assert isinstance(parsed, dict)
    assert "error" not in parsed


def test_decimal_serialized_as_string(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        return sample_result()

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    obs = payload["observations"][0]
    assert obs["period"] == "2020"
    assert obs["normalized_period_start"] == "2020-01-01"
    assert obs["period_semantics"] == "provider_year_label"
    assert isinstance(obs["value"], str)
    assert obs["decimal_scale"] == 0


def test_invalid_input_exit_2(capsys, test_settings):
    code = cli._main(
        [
            "--country",
            "1A",
            "--indicator",
            "SP.POP.TOTL",
            "--start-year",
            "2020",
            "--end-year",
            "2024",
        ]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "invalid_input"


def test_top_level_source_id(capsys, monkeypatch, test_settings):
    # §五：CLI 顶层 JSON 明确输出 source_id（固定 "2"）。
    async def _fetch(query):
        return sample_result()

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_id"] == "2"
    assert payload["provider_snapshot"]["source_id"] == "2"
    assert payload["indicator"]["source_id"] == "2"


def test_provider_not_ready_exit_3(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        raise WorldBankProviderNotReady("provider not found in source registry")

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "provider_not_ready"


def test_api_error_exit_4(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        raise WorldBankApiError("http status 500")

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "api_error"


def test_request_failed_stable_message(capsys, monkeypatch, test_settings):
    # 真实传输失败：stdout 只输出稳定非空错误，不输出 traceback / 底层细节。
    from app.macro.world_bank.errors import WorldBankRequestFailed

    async def _fetch(query):
        raise WorldBankRequestFailed("World Bank API request failed")

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "request_failed"
    assert payload["message"] == "World Bank API request failed"
    assert "ConnectError" not in payload["message"]
    assert "api.worldbank.org" not in payload["message"]


def test_country_metadata_mismatch_exit_4(capsys, monkeypatch, test_settings):
    # 响应国家与请求不一致（§三）→ malformed_response → exit 4；
    # stdout 只输出稳定脱敏 JSON（不含 traceback / 完整响应 / query / body），stderr 不泄漏 query。
    from app.macro.world_bank.errors import WorldBankMalformedResponse

    async def _fetch(query):
        raise WorldBankMalformedResponse("country metadata does not match requested code")

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 4
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"] == "malformed_response"
    assert payload["message"] == "country metadata does not match requested code"
    assert "traceback" not in captured.out.lower()
    # stdout 不含完整响应 / query / body 泄漏。
    for leaked in ("query", "body", "observations", "SP.POP.TOTL"):
        assert leaked not in captured.out
    # stderr 只含结构化日志，不输出 query / 完整响应正文。
    assert "SP.POP.TOTL" not in captured.err
    assert "country metadata does not match requested code" not in captured.err


def test_unexpected_error_exit_4(capsys, monkeypatch, test_settings):
    async def _fetch(query):
        raise RuntimeError("boom")

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    code = cli._main(_ARGS)
    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "unexpected_error"


def test_no_files_created(capsys, monkeypatch, test_settings, tmp_path):
    async def _fetch(query):
        return sample_result()

    _install_fake_provider(monkeypatch, _fetch, test_settings=test_settings)
    monkeypatch.chdir(tmp_path)
    code = cli._main(_ARGS)
    assert code == 0
    assert list(tmp_path.iterdir()) == []


def test_database_wired_and_disposed_without_connection(capsys, monkeypatch, test_settings):
    calls = {"factory": 0, "dispose": 0}
    received: list = []

    class _FakeDatabaseManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def session_factory(self):
            calls["factory"] += 1
            return object()

        async def dispose(self):
            calls["dispose"] += 1

    class _FakeProvider:
        def __init__(self, sessionmaker):
            received.append(sessionmaker)

        async def fetch(self, query):
            return sample_result()

    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.setattr(cli, "DatabaseManager", _FakeDatabaseManager)
    monkeypatch.setattr(cli, "WorldBankProvider", _FakeProvider)
    code = cli._main(_ARGS)
    assert code == 0
    assert calls["factory"] == 1
    assert calls["dispose"] == 1
    assert len(received) == 1
