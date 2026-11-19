"""Offline tests for the stock report: every network call is faked."""

import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

ENV_NAMES = [
    "GEMINI_API_KEY", "SENDER_EMAIL", "SENDER_PASSWORD", "RECEIVER_EMAIL",
    "GEMINI_MODELS", "DRY_RUN", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY",
]

NVDA_INFO = {
    "shortName": "NVIDIA Corporation",
    "sector": "Technology",
    "marketCap": 3_310_000_000_000,
    "currentPrice": 100.0,
    "fiftyTwoWeekHigh": 153.13,
    "fiftyTwoWeekLow": 75.61,
    "trailingPE": 53.24,
    "forwardPE": 30.1,
    "profitMargins": 0.5503,
    "returnOnEquity": 1.2377,
    "targetMeanPrice": 127.5,
}


def snapshot(**overrides):
  return main.build_snapshot({**NVDA_INFO, **overrides})


def sample_data(count=6):
  return {f"TICK{n}": snapshot(shortName=f"Company {n}") for n in range(count)}


def api_error(code, status="UNAVAILABLE", message="This model is currently experiencing high demand."):
  payload = {"error": {"code": code, "message": message, "status": status}}
  cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
  return cls(code, payload)


class FakeGemini:
  """Stands in for genai.Client; `script` maps model -> list of results/exceptions."""

  def __init__(self, script):
    self.script = {model: list(results) for model, results in script.items()}
    self.calls = []
    self.models = self

  def generate_content(self, model, contents):
    self.calls.append(model)
    result = self.script[model].pop(0)
    if isinstance(result, Exception):
      raise result
    return SimpleNamespace(text=result)


class FakeSMTP:
  sent = []
  login_error = None

  def __init__(self, host, port, timeout=None):
    self.host, self.port = host, port

  def __enter__(self):
    return self

  def __exit__(self, *exc_info):
    return False

  def login(self, user, password):
    if FakeSMTP.login_error:
      raise FakeSMTP.login_error

  def send_message(self, message, from_addr=None, to_addrs=None):
    FakeSMTP.sent.append((message, from_addr, to_addrs))


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
  for name in ENV_NAMES:
    monkeypatch.delenv(name, raising=False)
  FakeSMTP.sent = []
  FakeSMTP.login_error = None
  monkeypatch.setattr(main.smtplib, "SMTP_SSL", FakeSMTP)


@pytest.fixture
def full_env(monkeypatch):
  monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
  monkeypatch.setenv("SENDER_EMAIL", "bot@example.com")
  monkeypatch.setenv("SENDER_PASSWORD", "app-password")
  monkeypatch.setenv("RECEIVER_EMAIL", "a@example.com, b@example.com")


def no_sleep(_seconds):
  pass


# --- configuration ---------------------------------------------------------


def test_load_config_reads_everything(full_env):
  config = main.load_config()
  assert config["recipients"] == ["a@example.com", "b@example.com"]
  assert config["models"] == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
  assert config["dry_run"] is False


def test_load_config_lists_every_missing_secret():
  with pytest.raises(main.ReportError) as excinfo:
    main.load_config()
  for name in ["GEMINI_API_KEY", "SENDER_EMAIL", "SENDER_PASSWORD", "RECEIVER_EMAIL"]:
    assert name in str(excinfo.value)


def test_dry_run_does_not_need_email_settings(monkeypatch):
  monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
  monkeypatch.setenv("DRY_RUN", "true")
  assert main.load_config()["dry_run"] is True


def test_models_can_be_overridden(full_env, monkeypatch):
  monkeypatch.setenv("GEMINI_MODELS", " model-a ,model-b,")
  assert main.load_config()["models"] == ["model-a", "model-b"]


# --- market data -----------------------------------------------------------


def test_market_movers_come_from_the_screener(monkeypatch):
  quotes = [{"symbol": "NVDA"}, {"symbol": "BRK.B"}] + [
      {"symbol": f"T{n}"} for n in range(30)
  ]
  monkeypatch.setattr(main.yf, "screen", lambda name: {"quotes": quotes})
  tickers = main.get_market_movers()
  assert len(tickers) == main.MAX_TICKERS
  assert tickers[0] == "NVDA" and "BRK.B" not in tickers


def test_market_movers_fall_back_when_screener_fails(monkeypatch):
  def broken(name):
    raise RuntimeError("rate limited")

  monkeypatch.setattr(main.yf, "screen", broken)
  assert main.get_market_movers() == main.FALLBACK_TICKERS

  monkeypatch.setattr(main.yf, "screen", lambda name: {"quotes": []})
  assert main.get_market_movers() == main.FALLBACK_TICKERS


def test_snapshot_calculates_analyst_upside():
  data = snapshot()
  assert data["price"] == 100.0
  assert data["analyst_upside"] == pytest.approx(0.275)
  assert data["name"] == "NVIDIA Corporation"


def test_snapshot_handles_missing_and_odd_values():
  data = snapshot(trailingPE="Infinity", forwardPE=None, targetMeanPrice=None, profitMargins="n/a")
  assert data["trailing_pe"] is None
  assert data["forward_pe"] is None
  assert data["profit_margin"] is None
  assert data["analyst_upside"] is None


def test_snapshot_uses_regular_market_price_as_backup():
  assert snapshot(currentPrice=None, regularMarketPrice=42.0)["price"] == 42.0
  assert snapshot(currentPrice=None) is None
  assert main.build_snapshot({}) is None


def test_fundamentals_retry_and_skip_bad_tickers(monkeypatch):
  calls = []
  failures = {"FLAKY": 2, "DEAD": 99}

  def fake_ticker(symbol):
    calls.append(symbol)
    if failures.get(symbol, 0) > 0:
      failures[symbol] -= 1
      raise RuntimeError("Too Many Requests")
    info = {} if symbol == "EMPTY" else NVDA_INFO
    return SimpleNamespace(info=info)

  monkeypatch.setattr(main.yf, "Ticker", fake_ticker)
  data = main.get_fundamental_data(["GOOD", "FLAKY", "DEAD", "EMPTY"], sleep=no_sleep)
  assert list(data) == ["GOOD", "FLAKY"]
  assert calls.count("GOOD") == 1
  assert calls.count("FLAKY") == 3
  assert calls.count("DEAD") == 3


# --- formatting ------------------------------------------------------------


def test_formatters():
  assert main.fmt_money(1234.5) == "$1,234.50"
  assert main.fmt_money(None) == "N/A"
  assert main.fmt_ratio(53.24) == "53.2"
  assert main.fmt_pct(0.5503) == "55.0%"
  assert main.fmt_pct(-0.0312, signed=True) == "-3.1%"
  assert main.fmt_pct(0.275, signed=True) == "+27.5%"
  assert main.fmt_market_cap(3.31e12) == "$3.31T"
  assert main.fmt_market_cap(45.2e9) == "$45.20B"
  assert main.fmt_market_cap(None) == "N/A"
  assert main.fmt_range(75.61, 153.13) == "75.61\u2013153.13"
  assert main.fmt_range(None, 153.13) == "N/A\u2013153.13"
  assert main.fmt_range(None, None) == "N/A"
  assert main.short_name("Apple Inc.") == "Apple Inc."
  assert main.short_name("Advanced Micro Devices, Incorporated") == "Advanced Micro Devices, Inc\u2026"


def test_prompt_contains_the_data_and_the_three_sections():
  prompt = main.build_prompt({"NVDA": snapshot(), "XYZ": snapshot(shortName="", sector="", trailingPE=None)})
  assert "NVDA (NVIDIA Corporation, Technology)" in prompt
  assert "  Current Price: $100.00" in prompt
  assert "  Return on Equity (ROE): 123.8%" in prompt
  assert "  Analyst Upside: +27.5%" in prompt
  assert "\nXYZ\n" in prompt
  assert "  Trailing P/E Ratio: N/A" in prompt
  for section in ["Fundamentally Strong Stocks", "Top 5 Stocks with the Most Investment Potential",
                  "Specific Company Warning Signs"]:
    assert section in prompt


# --- Gemini ----------------------------------------------------------------


def test_gemini_first_try():
  client = FakeGemini({"primary": ["  report  "]})
  assert main.generate_with_fallback(client, ["primary", "backup"], "p", sleep=no_sleep) == ("report", "primary")
  assert client.calls == ["primary"]


def test_gemini_retries_overload_then_succeeds():
  client = FakeGemini({"primary": [api_error(503), "ok"]})
  delays = []
  assert main.generate_with_fallback(client, ["primary"], "p", sleep=delays.append) == ("ok", "primary")
  assert len(delays) == 1 and 10 <= delays[0] <= 13


def test_gemini_backoff_grows_between_attempts():
  client = FakeGemini({"primary": [api_error(503), api_error(429, "RESOURCE_EXHAUSTED"), "ok"]})
  delays = []
  main.generate_with_fallback(client, ["primary"], "p", attempts_per_model=3, sleep=delays.append)
  assert len(delays) == 2 and delays[1] > delays[0]


def test_gemini_falls_back_to_second_model():
  client = FakeGemini({"primary": [api_error(503), api_error(504, "DEADLINE_EXCEEDED")], "backup": ["from backup"]})
  assert main.generate_with_fallback(client, ["primary", "backup"], "p", sleep=no_sleep) == ("from backup", "backup")
  assert client.calls == ["primary", "primary", "backup"]


def test_gemini_skips_missing_model_without_retrying():
  client = FakeGemini({"retired": [api_error(404, "NOT_FOUND", "model is not found")], "backup": ["ok"]})
  delays = []
  assert main.generate_with_fallback(client, ["retired", "backup"], "p", sleep=delays.append) == ("ok", "backup")
  assert client.calls == ["retired", "backup"] and delays == []


def test_gemini_unavailable_after_all_rounds():
  client = FakeGemini({"primary": [api_error(503)] * 4, "backup": [None] * 4})
  delays = []
  with pytest.raises(main.GeminiUnavailableError, match="primary, backup"):
    main.generate_with_fallback(client, ["primary", "backup"], "p", sleep=delays.append)
  assert client.calls == ["primary", "primary", "backup", "backup"] * 2
  assert 60 in delays  # the longer pause before the second round


def test_gemini_stops_retrying_when_the_time_budget_is_spent():
  client = FakeGemini({"primary": [api_error(503)] * 4, "backup": [api_error(503)] * 4})
  ticks = iter(range(0, 10_000, 100))  # every look at the clock is 100s later
  with pytest.raises(main.GeminiUnavailableError):
    main.generate_with_fallback(
        client, ["primary", "backup"], "p", time_budget=150, sleep=no_sleep, clock=lambda: next(ticks))
  assert client.calls == ["primary", "primary"]


# --- email -----------------------------------------------------------------


def test_email_renders_markdown_and_includes_the_numbers():
  config = {"sender_email": "bot@example.com", "recipients": ["a@example.com"]}
  data = {"NVDA": snapshot(), "LOSS": snapshot(shortName="Loss & Co", targetMeanPrice=80.0)}
  today = datetime(2026, 11, 19, 9, 0, tzinfo=main.MARKET_TIMEZONE)
  report = "## 1. Fundamentally Strong Stocks\n\n- **NVDA**: strong margins"
  message = main.build_email(config, data, report, "gemini-3.6-flash", today=today)

  assert message["Subject"] == "📈 Daily Fundamental Stock Market Report - Thu, 19 Nov 2026"
  assert message["To"] == "a@example.com"

  html_part = message.get_body(preferencelist=("html",)).get_content()
  assert "<h2>1. Fundamentally Strong Stocks</h2>" in html_part
  assert "<strong>NVDA</strong>: strong margins" in html_part
  assert "**" not in html_part  # Markdown is rendered, not shown raw
  assert '<td>$127.50<br><span class="up">+27.5%</span></td>' in html_part
  assert '<td>$80.00<br><span class="down">-20.0%</span></td>' in html_part
  assert '<td>$100.00<br><span class="sub">75.61\u2013153.13</span></td>' in html_part
  assert "Loss &amp; Co" in html_part
  assert "not financial advice" in html_part
  assert "Analysis by gemini-3.6-flash" in html_part

  plain = message.get_body(preferencelist=("plain",)).get_content()
  assert "## 1. Fundamentally Strong Stocks" in plain
  assert "Ticker" in plain and "+27.5%" in plain and "$100.00" in plain


def test_nested_bullets_from_gemini_stay_nested():
  report = "## 2. Top 5\n- **1. NVDA**\n  - Analyst Upside: +27.5%\n  - Rationale: strong\n"
  html_body = main.render_html(report, {"NVDA": snapshot()}, "date", "footer")
  assert html_body.count("<ul>") == 2  # an outer list with one nested list
  assert "<li>Analyst Upside: +27.5%</li>" in html_body
  # Text that already uses 4 spaces (or has no nesting) is left alone.
  four = "- a\n    - b\n"
  assert main.normalize_list_indentation(four) == four
  assert main.normalize_list_indentation("- a\n- b\n") == "- a\n- b\n"


def test_email_without_ai_report_still_has_the_table():
  config = {"sender_email": "bot@example.com", "recipients": ["a@example.com"]}
  message = main.build_email(config, {"NVDA": snapshot()}, None, None)
  html_part = message.get_body(preferencelist=("html",)).get_content()
  assert "AI analysis was unavailable" in html_part
  assert "<strong>NVDA</strong>" in html_part
  assert "Analysis by" not in html_part


def test_text_table_is_aligned():
  lines = main.render_data_table_text({"NVDA": snapshot(), "A": snapshot(targetMeanPrice=None)}).splitlines()
  assert len({len(line) for line in lines}) == 1
  assert lines[0].startswith("Ticker") and lines[2].rstrip().endswith("N/A")


def test_send_email_reports_bad_password_clearly():
  FakeSMTP.login_error = main.smtplib.SMTPAuthenticationError(535, b"bad credentials")
  config = {"sender_email": "bot@example.com", "sender_password": "x", "recipients": ["a@example.com"]}
  message = main.build_email(config, {"NVDA": snapshot()}, "report", "m")
  with pytest.raises(main.ReportError, match="app password"):
    main.send_email_report(config, message, sleep=no_sleep)


# --- end to end ------------------------------------------------------------


def patch_pipeline(monkeypatch, gemini_result, data=None):
  data = sample_data() if data is None else data
  monkeypatch.setattr(main, "get_market_movers", lambda: list(data) or ["AAA", "BBB"])
  monkeypatch.setattr(main, "get_fundamental_data", lambda tickers: data)

  def fake_report(stock_data, api_key, models):
    if isinstance(gemini_result, Exception):
      raise gemini_result
    return gemini_result, models[0]

  monkeypatch.setattr(main, "generate_investment_report", fake_report)


def test_main_sends_report(full_env, monkeypatch, tmp_path, capsys):
  summary_file = tmp_path / "summary.md"
  monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
  patch_pipeline(monkeypatch, "## 1. Fundamentally Strong Stocks\n\n- **TICK0**")

  assert main.main() == 0

  message, from_addr, to_addrs = FakeSMTP.sent[0]
  assert from_addr == "bot@example.com"
  assert to_addrs == ["a@example.com", "b@example.com"]
  assert "Fundamentally Strong Stocks" in message.get_body(preferencelist=("html",)).get_content()
  summary = summary_file.read_text()
  assert "Emailed to 2 recipient(s)" in summary and "TICK0" in summary
  # Public repository: neither addresses nor the report itself go to the log.
  output = capsys.readouterr().out
  assert "example.com" not in output and "Fundamentally Strong" not in output
  assert "example.com" not in summary


def test_main_dry_run_prints_report_and_sends_nothing(monkeypatch, capsys):
  monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
  monkeypatch.setenv("DRY_RUN", "true")
  patch_pipeline(monkeypatch, "the report body")

  assert main.main() == 0
  assert FakeSMTP.sent == []
  output = capsys.readouterr().out
  assert "the report body" in output and "TICK0" in output


def test_main_sends_data_only_when_gemini_is_down(full_env, monkeypatch, capsys):
  monkeypatch.setenv("GITHUB_ACTIONS", "true")
  patch_pipeline(monkeypatch, main.GeminiUnavailableError("No Gemini model produced a report."))

  assert main.main() == 0

  html_part = FakeSMTP.sent[0][0].get_body(preferencelist=("html",)).get_content()
  assert "AI analysis was unavailable" in html_part and "TICK0" in html_part
  assert "::warning::" in capsys.readouterr().out


def test_main_refuses_to_report_on_too_little_data(full_env, monkeypatch, capsys):
  monkeypatch.setenv("GITHUB_ACTIONS", "true")
  called = []
  patch_pipeline(monkeypatch, "should not be used", data=sample_data(count=2))
  monkeypatch.setattr(main, "generate_investment_report", lambda *args: called.append(1))

  assert main.main() == 1
  assert called == [] and FakeSMTP.sent == []
  assert "::error::Yahoo Finance returned usable data for only 2" in capsys.readouterr().out


def test_main_fails_when_secrets_are_missing(monkeypatch, capsys):
  monkeypatch.setenv("GITHUB_ACTIONS", "true")
  assert main.main() == 1
  assert "::error::Missing required GitHub Secrets" in capsys.readouterr().out
