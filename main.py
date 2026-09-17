
import yfinance as yf
import yfscreen as yfs
import os
from google import genai

# Ensure your GEMINI_API_KEY is stored in your environment or GitHub Secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_market_movers():
    """Scan ALL US stocks and filter for the top active movers based on volume and size."""
    filters = [
        ["eq", ["region", "us"]],
        ["gt", ["dayvolume", 5000000]], # Daily trading volume greater than 5 Million
        ["btwn", ["intradaymarketcap", 2e9, 1e12]] # Market cap between $2B and $1T
    ]
    query = yfs.create_query(filters)
    payload = yfs.create_payload("equity", query)
    
    # Get the top 15 results from the screener
    data = yfs.get_data(payload)
    tickers = data['symbol'].head(15).tolist()
    return tickers

def get_fundamental_data(tickers):
    """Fetch deep quantitative and fundamental data for each stock."""
    stock_data = {}
    
    for ticker in tickers:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # Extracting key fundamentals for the AI to evaluate
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
    """Feed the quantitative data to the latest Gemini model for fundamental analysis."""
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
    
    # Using the latest model: gemini-3.7-flash
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
    )
    return response.text

if __name__ == "__main__":
    print("1. Scanning the entire market for top movers...")
    top_tickers = get_market_movers()
    print(f"Screened Tickers: {top_tickers}")
    
    print("2. Fetching fundamental financial metrics...")
    fundamentals = get_fundamental_data(top_tickers)
    
    print("3. Generating AI Analysis using Gemini 3.7 Flash...")
    final_report = generate_investment_report(fundamentals)
    
    print("\n--- FUNDAMENTAL STOCK ANALYSIS REPORT ---\n")
    print(final_report)
