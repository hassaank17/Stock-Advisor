"""Daily fundamental stock report.

Screens Yahoo Finance for the most active US stocks, collects their
fundamentals, asks Gemini for a structured investment report and emails it as
HTML + plain text, together with the numbers the report is based on.

Settings come from environment variables (GitHub Actions secrets/variables):

  GEMINI_API_KEY    Google Gemini API key
  SENDER_EMAIL      Gmail address the report is sent from
  SENDER_PASSWORD   Gmail app password for SENDER_EMAIL
  RECEIVER_EMAIL    Recipient address(es), comma-separated
  GEMINI_MODELS     Optional. Models to try in order, comma-separated
  DRY_RUN           Optional. "true" builds the report without emailing it
"""

import html
import math
import os
import random
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import markdown
import yfinance as yf
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

MAX_TICKERS = 15
# Below this many stocks with usable data the report would be guesswork.
MIN_TICKERS_WITH_DATA = 5
MARKET_TIMEZONE = ZoneInfo("America/New_York")
SMTP_TIMEOUT = 30  # seconds

# Tried in order. When a model is overloaded (503) or rate limited (429) the
# next one takes over, so one busy model no longer means a missed report.
DEFAULT_GEMINI_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite"
GEMINI_TIMEOUT_MS = 120_000
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}

FALLBACK_TICKERS = [
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "TSLA",
    "META",
    "AMD",
    "AVGO",
    "PLTR",
    "INTC",
    "BABA",
    "NFLX",
    "JPM",
    "LLY",
]

DISCLAIMER = (
    "Automated report built from Yahoo Finance data and an AI model. "
    "For information only - not financial advice."
)

EMAIL_STYLE = """
  body { margin: 0; padding: 0; background: #f3f4f6; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 24px 12px; }
  .card { background: #ffffff; border-radius: 10px; padding: 28px 28px 20px;
          font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
          font-size: 16px; line-height: 1.6; color: #1f2937; }
  h1 { font-size: 24px; margin: 0 0 4px; color: #111827; }
  .date { margin: 0 0 20px; color: #6b7280; font-size: 14px; }
  h2 { font-size: 19px; margin: 28px 0 10px; padding-bottom: 6px;
       border-bottom: 2px solid #e5e7eb; color: #111827; }
  h3 { font-size: 17px; margin: 22px 0 8px; color: #111827; }
  li { margin-bottom: 6px; }
  blockquote { margin: 0 0 16px; padding: 10px 14px; background: #fef3c7;
               border-left: 4px solid #f59e0b; color: #78350f; }
  blockquote p { margin: 0; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; line-height: 1.45; }
  th, td { padding: 7px 8px; border-bottom: 1px solid #e5e7eb; text-align: right;
           white-space: nowrap; vertical-align: top; }
  th { background: #f9fafb; color: #374151; font-weight: 600; }
  th:first-child, td:first-child { text-align: left; white-space: normal; }
  .sub { color: #6b7280; font-size: 11px; font-weight: 400; }
  .up { color: #047857; font-weight: 600; }
  .down { color: #b91c1c; font-weight: 600; }
  .footer { margin-top: 28px; padding-top: 12px; border-top: 1px solid #e5e7eb;
            color: #9ca3af; font-size: 12px; }
  @media (max-width: 480px) {
    .wrap { padding: 12px 6px; }
    .card { padding: 20px 14px 16px; }
    th, td { padding: 7px 4px; }
  }
"""


class ReportError(Exception):
  """A problem that should stop the run with a clear message."""


class GeminiUnavailableError(Exception):
  """No configured Gemini model produced a usable answer."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def env(name):
  return os.environ.get(name, "").strip()


def split_csv(value):
  return [item.strip() for item in value.split(",") if item.strip()]


def load_config():
  """Read settings from the environment and fail early if any are missing."""
  config = {
      "gemini_api_key": env("GEMINI_API_KEY"),
      "sender_email": env("SENDER_EMAIL"),
      "sender_password": env("SENDER_PASSWORD"),
      "recipients": split_csv(env("RECEIVER_EMAIL")),
      "models": (
          split_csv(env("GEMINI_MODELS")) or split_csv(DEFAULT_GEMINI_MODELS)
      ),
      "dry_run": env("DRY_RUN").lower() in {"1", "true", "yes", "on"},
  }

  required = {"GEMINI_API_KEY": config["gemini_api_key"]}
  if not config["dry_run"]:
    required.update({
        "SENDER_EMAIL": config["sender_email"],
        "SENDER_PASSWORD": config["sender_password"],
        "RECEIVER_EMAIL": config["recipients"],
    })
  missing = [name for name, value in required.items() if not value]
  if missing:
    raise ReportError(
        f"Missing required GitHub Secrets: {', '.join(missing)}. Please add"
        " them under Settings > Secrets and variables > Actions (or set"
        " DRY_RUN=true to build the report without emailing it)."
    )
  return config


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def get_market_movers():
  """Fetch the top active market movers directly through yfinance."""
  print("Scanning the market for high-volume active stocks...")
  try:
    screener = yf.screen("most_actives")
    quotes = screener.get("quotes", [])
    tickers = [item["symbol"] for item in quotes if "." not in item["symbol"]][
        :MAX_TICKERS
    ]
    if not tickers:
      raise ValueError("No tickers returned from screener.")
    return tickers
  except Exception as e:
    print(f"Screener fallback triggered due to: {e}")
    return list(FALLBACK_TICKERS)


def to_number(value):
  """Return value as a finite float, or None for missing/odd values."""
  if value is None or isinstance(value, bool):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  return number if math.isfinite(number) else None


def build_snapshot(info):
  """Pick the metrics the report uses out of a yfinance `info` dict.

  Returns None when there is no price, i.e. nothing usable came back.
  """
  price = to_number(info.get("currentPrice"))
  if price is None:
    price = to_number(info.get("regularMarketPrice"))
  if not price or price <= 0:
    return None

  target = to_number(info.get("targetMeanPrice"))
  return {
      "name": info.get("shortName") or info.get("longName") or "",
      "sector": info.get("sector") or "",
      "market_cap": to_number(info.get("marketCap")),
      "price": price,
      "high_52w": to_number(info.get("fiftyTwoWeekHigh")),
      "low_52w": to_number(info.get("fiftyTwoWeekLow")),
      "trailing_pe": to_number(info.get("trailingPE")),
      "forward_pe": to_number(info.get("forwardPE")),
      "profit_margin": to_number(info.get("profitMargins")),
      "roe": to_number(info.get("returnOnEquity")),
      "target_price": target,
      # Worked out here so the model never has to do the arithmetic itself.
      "analyst_upside": (target / price - 1) if target else None,
  }


def fetch_snapshot(ticker, attempts=3, sleep=time.sleep):
  """Fetch one ticker, retrying because Yahoo regularly rate limits.

  Returns None if no usable data came back after all attempts.
  """
  problem = "no price data returned"
  for attempt in range(1, attempts + 1):
    try:
      snapshot = build_snapshot(yf.Ticker(ticker).info or {})
      if snapshot:
        return snapshot
    except Exception as err:
      problem = f"{type(err).__name__}: {err}"
    if attempt < attempts:
      sleep(3 * attempt)
  print(f"Warning: Failed to fetch {ticker}: {problem}")
  return None


def get_fundamental_data(tickers, sleep=time.sleep):
  stock_data = {}
  print(f"Fetching fundamental data for {len(tickers)} symbols...")
  for ticker in tickers:
    snapshot = fetch_snapshot(ticker, sleep=sleep)
    if snapshot:
      stock_data[ticker] = snapshot
  print(f"Got fundamentals for {len(stock_data)} of {len(tickers)} symbols.")
  return stock_data


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_money(value):
  return "N/A" if value is None else f"${value:,.2f}"


def fmt_ratio(value):
  return "N/A" if value is None else f"{value:,.1f}"


def fmt_pct(value, signed=False):
  if value is None:
    return "N/A"
  return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def fmt_range(low, high):
  if low is None and high is None:
    return "N/A"
  low_text = "N/A" if low is None else f"{low:,.2f}"
  high_text = "N/A" if high is None else f"{high:,.2f}"
  return f"{low_text}\u2013{high_text}"


def fmt_market_cap(value):
  if value is None:
    return "N/A"
  for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
    if abs(value) >= limit:
      return f"${value / limit:.2f}{suffix}"
  return f"${value:,.0f}"


def format_data_for_prompt(stock_data):
  blocks = []
  for ticker, d in stock_data.items():
    details = ", ".join(part for part in (d["name"], d["sector"]) if part)
    blocks.append("\n".join([
        f"{ticker}" + (f" ({details})" if details else ""),
        f"  Market Cap: {fmt_market_cap(d['market_cap'])}",
        f"  Current Price: {fmt_money(d['price'])}",
        f"  52-Week High: {fmt_money(d['high_52w'])}",
        f"  52-Week Low: {fmt_money(d['low_52w'])}",
        f"  Trailing P/E Ratio: {fmt_ratio(d['trailing_pe'])}",
        f"  Forward P/E Ratio: {fmt_ratio(d['forward_pe'])}",
        f"  Profit Margin: {fmt_pct(d['profit_margin'])}",
        f"  Return on Equity (ROE): {fmt_pct(d['roe'])}",
        f"  Analyst Target Price: {fmt_money(d['target_price'])}",
        f"  Analyst Upside: {fmt_pct(d['analyst_upside'], signed=True)}",
    ]))
  return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def build_prompt(stock_data):
  return f"""You are an expert quantitative financial analyst. I screened the US market for active large/mid-cap stocks today.
Here is their current fundamental data:

{format_data_for_prompt(stock_data)}

Based strictly on these financial metrics, provide a structured investment report covering:
1. Fundamentally Strong Stocks: Which companies exhibit strong quality metrics across profitability, capital efficiency, and earnings support (positive P/E ratios, robust margins, and strong ROE).
2. Top 5 Stocks with the Most Investment Potential: Rank these by combining strong fundamentals/valuations with analyst price target upside. Include the Analyst Upside and Rationale for each.
3. Specific Company Warning Signs: Group these into three categories:
    A. Negative Earnings & Capital Destruction (Negative ROE / Negative Margins).
    B. Overvaluation Relative to Earnings Performance.
    C. Thin Profit Margins & Low ROE.

Rules:
- Use only the figures above. "N/A" means the figure is unavailable; never estimate or invent a number.
- Analyst Upside is already calculated as (Analyst Target Price / Current Price) - 1. Quote it as given instead of recalculating it.
- Format the report in Markdown: a ## heading for each of the three sections, **bold** ticker symbols, and - bullet points.
- Output only the report itself, with no introduction or closing remarks.
"""


def describe_error(exc):
  if isinstance(exc, genai_errors.APIError):
    return f"{exc.code} {exc.status}: {exc.message}"[:300].rstrip(".")
  return f"{type(exc).__name__}: {exc}"[:300].rstrip(".")


def is_transient(exc):
  """True for errors worth retrying: overloads, rate limits, network hiccups."""
  if isinstance(exc, genai_errors.APIError):
    return exc.code in TRANSIENT_STATUS_CODES
  return True


def generate_with_fallback(
    client,
    models,
    prompt,
    attempts_per_model=3,
    rounds=2,
    base_delay=10,
    round_delay=60,
    sleep=time.sleep,
):
  """Ask Gemini for text, retrying transient errors and falling back across models.

  Each model gets a few attempts with exponential backoff before the next
  model is tried. A model that fails permanently (retired, not found, bad
  request) is dropped immediately. If every model is still failing, the whole
  list is tried once more after a longer pause.

  Returns (text, model). Raises GeminiUnavailableError if nothing worked.
  """
  candidates = list(models)
  last_error = None

  for round_number in range(1, rounds + 1):
    for model in list(candidates):
      for attempt in range(1, attempts_per_model + 1):
        try:
          response = client.models.generate_content(
              model=model, contents=prompt
          )
          text = (response.text or "").strip()
          if text:
            return text, model
          raise ValueError("Gemini returned an empty response")
        except Exception as exc:  # noqa: BLE001 - classified just below
          last_error = exc
          if not is_transient(exc):
            print(
                f"[{model}] {describe_error(exc)} - not retryable, skipping"
                " this model."
            )
            candidates.remove(model)
            break
          if attempt < attempts_per_model:
            delay = base_delay * 2 ** (attempt - 1) + random.uniform(0, 3)
            print(
                f"[{model}] attempt {attempt}/{attempts_per_model} failed:"
                f" {describe_error(exc)}. Retrying in {delay:.0f}s..."
            )
            sleep(delay)
          else:
            print(
                f"[{model}] attempt {attempt}/{attempts_per_model} failed:"
                f" {describe_error(exc)}. Moving on."
            )

    if not candidates:
      break
    if round_number < rounds:
      print(f"No model answered. Waiting {round_delay}s before trying again...")
      sleep(round_delay)

  raise GeminiUnavailableError(
      f"No Gemini model produced a report (tried: {', '.join(models)}). Last"
      f" error: {describe_error(last_error)}"
  )


def generate_investment_report(stock_data, api_key, models):
  """Write the report with Gemini. Returns (markdown, model)."""
  print("Generating AI report with Gemini...")
  client = genai.Client(
      api_key=api_key,
      http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
  )
  return generate_with_fallback(client, models, build_prompt(stock_data))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

# The HTML table stacks these two per cell (Price over 52W Range, P/E over
# Fwd P/E, ...) so it stays narrow enough to read on a phone.
TABLE_COLUMNS = [
    ("Price", lambda d: fmt_money(d["price"])),
    ("52W Range", lambda d: fmt_range(d["low_52w"], d["high_52w"])),
    ("P/E", lambda d: fmt_ratio(d["trailing_pe"])),
    ("Fwd P/E", lambda d: fmt_ratio(d["forward_pe"])),
    ("Margin", lambda d: fmt_pct(d["profit_margin"])),
    ("ROE", lambda d: fmt_pct(d["roe"])),
    ("Target", lambda d: fmt_money(d["target_price"])),
    ("Upside", lambda d: fmt_pct(d["analyst_upside"], signed=True)),
]
MAX_NAME_LENGTH = 28


def short_name(name):
  if len(name) <= MAX_NAME_LENGTH:
    return name
  return name[: MAX_NAME_LENGTH - 1].rstrip() + "\u2026"


def render_data_table_html(stock_data):
  pairs = list(zip(TABLE_COLUMNS[0::2], TABLE_COLUMNS[1::2]))
  head = "<th>Stock</th>" + "".join(
      f'<th>{top}<br><span class="sub">{bottom}</span></th>'
      for (top, _), (bottom, _) in pairs
  )
  rows = []
  for ticker, d in stock_data.items():
    cells = [
        f"<td><strong>{html.escape(ticker)}</strong>"
        f'<br><span class="sub">{html.escape(short_name(d["name"]))}</span></td>'
    ]
    for (_, render_top), (bottom, render_bottom) in pairs:
      css = "sub"
      if bottom == "Upside" and d["analyst_upside"] is not None:
        css = "up" if d["analyst_upside"] >= 0 else "down"
      cells.append(
          f"<td>{render_top(d)}"
          f'<br><span class="{css}">{render_bottom(d)}</span></td>'
      )
    rows.append("<tr>" + "".join(cells) + "</tr>")
  return (
      "<table>\n"
      f"<thead><tr>{head}</tr></thead>\n"
      "<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>"
  )


def render_data_table_text(stock_data):
  header = ["Ticker"] + [label for label, _ in TABLE_COLUMNS]
  rows = [
      [ticker] + [render(d) for _, render in TABLE_COLUMNS]
      for ticker, d in stock_data.items()
  ]
  table = [header] + rows
  widths = [max(len(row[i]) for row in table) for i in range(len(header))]
  return "\n".join(
      "  ".join(
          cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
          for i, cell in enumerate(row)
      )
      for row in table
  )


def render_html(report_markdown, stock_data, date_label, footer):
  body = markdown.markdown(report_markdown, extensions=["extra"])
  return (
      '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
      f"<style>{EMAIL_STYLE}</style>\n</head>\n<body>\n"
      '<div class="wrap"><div class="card">\n'
      "<h1>Daily Fundamental Stock Market Report</h1>\n"
      f'<p class="date">{html.escape(date_label)}</p>\n'
      f"{body}\n"
      "<h2>The numbers behind this report</h2>\n"
      f"{render_data_table_html(stock_data)}\n"
      f'<p class="footer">{html.escape(footer)}</p>\n'
      "</div></div>\n</body>\n</html>\n"
  )


UNAVAILABLE_NOTE = (
    "> **Note:** The AI analysis was unavailable today, so this email contains"
    " the screened stocks and their fundamentals only."
)


def build_email(config, stock_data, report_markdown, model, today=None):
  """Build the report email with a plain-text part and an HTML part.

  `report_markdown` is None when Gemini was unavailable; the data table is
  still sent so the day's numbers are not lost.
  """
  today = today or datetime.now(MARKET_TIMEZONE)
  date_label = today.strftime("%A, %d %B %Y")
  report_markdown = report_markdown or UNAVAILABLE_NOTE
  footer = DISCLAIMER + (f" Analysis by {model}." if model else "")

  message = EmailMessage()
  message["Subject"] = (
      "📈 Daily Fundamental Stock Market Report -"
      f" {today.strftime('%a, %d %b %Y')}"
  )
  message["From"] = config["sender_email"]
  message["To"] = ", ".join(config["recipients"])
  message.set_content(
      f"Daily Fundamental Stock Market Report - {date_label}\n\n"
      f"{report_markdown}\n\n"
      "The numbers behind this report\n"
      "------------------------------\n"
      f"{render_data_table_text(stock_data)}\n\n--\n{footer}\n"
  )
  message.add_alternative(
      render_html(report_markdown, stock_data, date_label, footer),
      subtype="html",
  )
  return message


def send_email_report(config, message, attempts=2, sleep=time.sleep):
  """Send the report through Gmail SMTP, retrying once on a dropped connection."""
  # Only the count is logged: this repository is public and so are its run logs.
  print(f"Dispatching report to {len(config['recipients'])} recipient(s)...")
  for attempt in range(1, attempts + 1):
    try:
      with smtplib.SMTP_SSL(
          "smtp.gmail.com", 465, timeout=SMTP_TIMEOUT
      ) as server:
        server.login(config["sender_email"], config["sender_password"])
        server.send_message(
            message,
            from_addr=config["sender_email"],
            to_addrs=config["recipients"],
        )
      break
    except smtplib.SMTPAuthenticationError as exc:
      raise ReportError(
          "Gmail rejected the login. SENDER_PASSWORD must be a Gmail app"
          " password (not the account password) for SENDER_EMAIL."
      ) from exc
    except (smtplib.SMTPException, OSError) as exc:
      if attempt == attempts:
        raise ReportError(
            f"Could not send the email: {type(exc).__name__}: {exc}"
        ) from exc
      print(
          f"Email attempt {attempt}/{attempts} failed"
          f" ({type(exc).__name__}). Retrying in 10s..."
      )
      sleep(10)
  print("Email successfully sent!")


# ---------------------------------------------------------------------------
# GitHub Actions helpers
# ---------------------------------------------------------------------------


def annotate(level, text):
  """Print a message that GitHub Actions highlights as an error/warning."""
  if os.environ.get("GITHUB_ACTIONS") == "true":
    print(f"::{level}::{' '.join(text.split())}")
  else:
    print(f"{level.upper()}: {text}")


def write_job_summary(text):
  """Show a short status on the workflow run page (no-op outside Actions)."""
  path = os.environ.get("GITHUB_STEP_SUMMARY")
  if not path:
    return
  with open(path, "a", encoding="utf-8") as handle:
    handle.write(text + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run():
  config = load_config()
  top_tickers = get_market_movers()
  fundamentals = get_fundamental_data(top_tickers)
  if len(fundamentals) < MIN_TICKERS_WITH_DATA:
    raise ReportError(
        f"Yahoo Finance returned usable data for only {len(fundamentals)} of"
        f" {len(top_tickers)} stocks, which is too few for a meaningful"
        " report. This is usually temporary rate limiting; re-run the workflow"
        " later."
    )

  try:
    report, model = generate_investment_report(
        fundamentals, config["gemini_api_key"], config["models"]
    )
    print(f"Report written by {model}.")
  except GeminiUnavailableError as exc:
    annotate("warning", f"{exc} Sending the data table without the analysis.")
    report, model = None, None

  message = build_email(config, fundamentals, report, model)
  if config["dry_run"]:
    print("\n--- Report (dry run, no email sent) ---\n")
    print(message.get_body(preferencelist=("plain",)).get_content())
    status = "Dry run - no email sent"
  else:
    send_email_report(config, message)
    status = f"Emailed to {len(config['recipients'])} recipient(s)"

  write_job_summary(
      f"**{status}.**\n\n"
      f"- Stocks analysed ({len(fundamentals)}): {', '.join(fundamentals)}\n"
      f"- Analysis by: {model or 'none (Gemini unavailable, data table only)'}"
  )


def main():
  try:
    run()
  except ReportError as exc:
    annotate("error", str(exc))
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
