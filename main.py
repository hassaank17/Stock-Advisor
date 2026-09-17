import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import yfinance as yf
from google import genai

# Fetch environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")


def verify_env_variables():
  required = {
      "GEMINI_API_KEY": GEMINI_API_KEY,
      "SENDER_EMAIL": SENDER_EMAIL,
      "EMAIL_PASSWORD": EMAIL_PASSWORD,
      "RECEIVER_EMAIL": RECEIVER_EMAIL,
  }
  missing = [k for k, v in required.items() if not v]
  if missing:
    raise ValueError(
        f"Missing required GitHub Secrets: {', '.join(missing)}. Please add"
        " them under Settings > Secrets and variables > Actions."
    )


def get_market_movers():
  """Fetch the top active market movers directly through yfinance."""
  print("Scanning the market for high-volume active stocks...")
  try:
    # Use native yfinance predefined screener to bypass external scraper bans
    screener = yf.screen("most_actives")
    quotes = screener.get("quotes", [])
    tickers = [item["symbol"] for item in quotes if "." not in item["symbol"]][
        :15
    ]
    if not tickers:
      raise ValueError("No tickers returned from screener.")
    return tickers
  except Exception as e:
    print(f"Screener fallback triggered due to: {e}")
    # Fallback to high-liquidity large-cap universe if Yahoo blocks screener API
    return [
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


def get_fundamental_data(tickers):
  stock_data = {}
  print(f"Fetching fundamental data for {len(tickers)} symbols...")
  for ticker in tickers:
    try:
      stock = yf.Ticker(ticker)
      info = stock.info
      stock_data[ticker] = {
          "Current Price": info.get("currentPrice", "N/A"),
          "52-Week High": info.get("fiftyTwoWeekHigh", "N/A"),
          "52-Week Low": info.get("fiftyTwoWeekLow", "N/A"),
          "Trailing P/E Ratio": info.get("trailingPE", "N/A"),
          "Forward P/E Ratio": info.get("forwardPE", "N/A"),
          "Profit Margin": info.get("profitMargins", "N/A"),
          "Return on Equity (ROE)": info.get("returnOnEquity", "N/A"),
          "Analyst Target Price": info.get("targetMeanPrice", "N/A"),
      }
    except Exception as err:
      print(f"Warning: Failed to fetch {ticker}: {err}")
  return stock_data


def generate_investment_report(fundamental_data):
  print("Generating AI report with Gemini...")
  client = genai.Client(api_key=GEMINI_API_KEY)
  prompt = f"""
    You are an expert quantitative financial analyst. I screened the US market for active large/mid-cap stocks today.
    Here is their current fundamental data: 
    {fundamental_data}
    
    Based strictly on these financial metrics, provide a structured investment report:
    1. Which stocks are fundamentally strong right now and why (reference P/E, margins, and ROE).
    2. The Top 5 stocks with the most investment potential based on valuation and analyst upside.
    3. Specific company warning signs (e.g., overvaluation, negative earnings, or razor-thin margins).
    """
  response = client.models.generate_content(
      model="gemini-2.5-flash",
      contents=prompt,
  )
  return response.text


def send_email_report(report_content):
  print(f"Dispatching report from {SENDER_EMAIL} to {RECEIVER_EMAIL}...")
  msg = MIMEMultipart()
  msg["From"] = SENDER_EMAIL
  msg["To"] = RECEIVER_EMAIL
  msg["Subject"] = "📈 Daily Fundamental Stock Market Report"
  msg.attach(MIMEText(report_content, "plain"))

  with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(SENDER_EMAIL, EMAIL_PASSWORD)
    server.send_message(msg)
  print("Email successfully sent!")


if __name__ == "__main__":
  verify_env_variables()
  top_tickers = get_market_movers()
  fundamentals = get_fundamental_data(top_tickers)
  report = generate_investment_report(fundamentals)
  send_email_report(report)
