import yfinance as yf
import yfscreen as yfs
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_market_movers():
    filters = [
        ["eq", ["region", "us"]],
        ["gt", ["dayvolume", 5000000]],
        ["btwn", ["intradaymarketcap", 2e9, 1e12]]
    ]
    query = yfs.create_query(filters)
    payload = yfs.create_payload("equity", query)
    data = yfs.get_data(payload)
    return data['symbol'].head(15).tolist()

def get_fundamental_data(tickers):
    stock_data = {}
    for ticker in tickers:
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
            "Analyst Target Price": info.get("targetMeanPrice", "N/A")
        }
    return stock_data

def generate_investment_report(fundamental_data):
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are an expert quantitative financial analyst. I screened the US market for today's highest volume mid-to-large cap stocks.
    Here is their current fundamental data: 
    {fundamental_data}
    
    Based strictly on these financial metrics, provide a structured investment report covering:
    1. Which stocks are fundamentally strong right now and why (reference metrics like P/E, margins, and ROE).
    2. The Top 5 stocks with the most investment potential based on valuation and upside to analyst targets.
    3. Specific company warning signs (e.g., overvaluation, poor margins).
    """
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )
    return response.text

def send_email_report(report_content):
    sender = os.getenv("SENDER_EMAIL")
    password = os.getenv("EMAIL_PASSWORD")
    receiver = os.getenv("RECEIVER_EMAIL")

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = "📈 Daily Fundamental Stock Market Report"
    msg.attach(MIMEText(report_content, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)
    print("Email sent successfully!")

if __name__ == "__main__":
    print("1. Scanning market...")
    top_tickers = get_market_movers()
    
    print("2. Fetching metrics...")
    fundamentals = get_fundamental_data(top_tickers)
    
    print("3. Generating analysis...")
    final_report = generate_investment_report(fundamentals)
    
    print("4. Dispatching email...")
    send_email_report(final_report)
