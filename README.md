# Stock-Advisor

A GitHub Actions job that emails an AI-written fundamental stock report every weekday.

1. Screens Yahoo Finance for the 15 most active US stocks (falling back to a fixed
   list of large caps if the screener is unavailable).
2. Collects each stock's fundamentals: price, 52-week range, trailing and forward
   P/E, profit margin, return on equity, analyst target price and the upside to it.
3. Asks Google Gemini for a structured report: fundamentally strong stocks, the top 5
   by investment potential, and company-specific warning signs.
4. Emails the report as formatted HTML (with a plain-text copy) through Gmail,
   followed by a table of the numbers the report is based on.

> The report is generated automatically from Yahoo Finance data and an AI model.
> It is for information only and is not financial advice.

## Schedule

The workflow in [`.github/workflows/daily_scan.yml`](.github/workflows/daily_scan.yml)
runs **Monday to Friday at 13:47 UTC**. GitHub does not guarantee the exact start time
of scheduled runs; the minute is deliberately off the hour because runs queued at `:00`
are delayed the most. To change the time, edit the `cron` line.

GitHub switches off scheduled workflows in public repositories after 60 days without
repository activity. The `keepalive` job in the workflow prevents that. If the schedule
is ever disabled anyway, turn it back on under **Actions > Stock Scanner Daily Analysis >
Enable workflow**.

## Setup

Add these under **Settings > Secrets and variables > Actions > Secrets**:

| Secret | What it is |
| --- | --- |
| `GEMINI_API_KEY` | API key from Google AI Studio |
| `SENDER_EMAIL` | Gmail address the report is sent from |
| `SENDER_PASSWORD` | A Gmail [app password](https://myaccount.google.com/apppasswords) for that address (not the account password) |
| `RECEIVER_EMAIL` | One or more recipient addresses, comma-separated |

Optional, under the **Variables** tab:

| Variable | What it does |
| --- | --- |
| `GEMINI_MODELS` | Comma-separated Gemini models to try, in order. Default: `gemini-3.6-flash,gemini-3.5-flash-lite` |

## Running it by hand

**Actions > Stock Scanner Daily Analysis > Run workflow.** Tick **Dry run** to build
the report and print it in the run log without sending any email. The **models** box
overrides the model list for that one run, which is handy for trying a new model.

This repository is public, so its run logs are too. Normal runs keep the report and
all email addresses out of the log; only a dry run prints the report.

## When something goes wrong

- **Gemini is overloaded or rate limited (503 / 429):** each model is retried with
  increasing waits, then the next model in the list is tried. If none answers, the
  email still goes out with the data table and the run shows a warning.
- **A model has been retired (404):** it is skipped and the next one is used.
  Update `GEMINI_MODELS` when you see that warning.
- **Yahoo Finance rate limits:** each stock is retried. If fewer than 5 stocks return
  usable data the run fails instead of sending a report built on almost nothing.
- **A missing secret or a Gmail failure:** the run fails with a one-line explanation
  at the top of the run page.

## Running locally

```bash
pip install -r requirements.txt
```

```bash
GEMINI_API_KEY=... DRY_RUN=true python main.py
```

## Tests

The tests are offline (every network call is faked) and run on each push through
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

```bash
pip install pytest
```

```bash
python -m pytest -q
```
