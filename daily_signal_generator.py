import yfinance as yf
import numpy as np
import pandas as pd
import datetime
import os
import requests
import sys

# --- [전략 설정] ---
TICKERS = [
    '102110.KS', '283580.KS', '453810.KS',
    '148070.KS', '385560.KS'
]
BASE_WEIGHTS = {t: 0.20 for t in TICKERS} # 모두 20% 균등 비중
N_BAND = 0.03 # 3% 이격도
MA_WINDOWS = [20, 120, 200]
SCALAR_MAP = {3: 1.0, 2: 0.75, 1: 0.50, 0: 0.0}

# 티커 명칭 매핑 (보고서 가독성 향상)
TICKER_NAMES = {
    '102110.KS': 'TIGER 200 (KOSPI200)', '283580.KS': 'KODEX ChinaCSI300', 
    '453810.KS': 'KODEX IndiaNifty50', '148070.KS': 'KIWOOM KTB 10Y', 
    '385560.KS': 'RISE KTB 30Y Enhanced', 'Cash': 'Cash (Not Invested)'
}

# --- Performance Calculation Function (for reporting) ---
def get_cagr(portfolio_returns):
    """Calculates Compound Annual Growth Rate (CAGR)"""
    total_return = (1 + portfolio_returns).prod()
    num_trading_days = len(portfolio_returns)
    num_years = num_trading_days / 252
    if num_years <= 0: return 0
    cagr = (total_return) ** (1 / num_years) - 1
    return cagr

# --- Core MA Strategy Execution Function ---
def run_ma_strategy_for_date(target_date):
    """
    Executes the MA strategy based on data up to the target date and returns the final portfolio state.
    """
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Data analysis started. Base date: {target_date.strftime('%Y-%m-%d')}")
    
    # 1. Download Data (Need sufficient historical data for MA calculation)
    end_date_for_download = target_date + datetime.timedelta(days=1)
    
    # Download data from a wide starting point to ensure 200-day MA calculation is possible
    data_full = yf.download(TICKERS, start="2022-01-01", end=end_date_for_download.strftime('%Y-%m-%d'), auto_adjust=True)
    prices_df = data_full['Close']
    
    # Data validation and refinement
    if prices_df.empty or prices_df.dropna(axis=0, how='any').empty:
        return None, "Data download failed or insufficient data."
        
    prices_df = prices_df.dropna(axis=0, how='any')
    
    # Extract data for the final date (closest valid trading day to target_date)
    if target_date.strftime('%Y-%m-%d') not in prices_df.index.strftime('%Y-%m-%d'):
        last_valid_date = prices_df.index[-1]
        prices_df = prices_df.loc[:last_valid_date]
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Base date adjusted: {last_valid_date.strftime('%Y-%m-%d')} (Due to non-trading day)")
    else:
        prices_df = prices_df.loc[:target_date.strftime('%Y-%m-%d')]

    if prices_df.empty or len(prices_df) < max(MA_WINDOWS):
        return None, "Insufficient data (200 days) for MA calculation."

    # 2. MA and Band Calculation (Based on the last trading day)
    latest_prices = prices_df.iloc[-1]
    today_scores = pd.Series(0, index=TICKERS)
    
    # 3. Calculate Daily Score and Determine Weight
    for ticker in TICKERS:
        score = 0
        for window in MA_WINDOWS:
            # Calculate MA line based on the last 'window' days including the latest price
            ma_line = prices_df[ticker].iloc[-window:].mean()
            upper = ma_line * (1.0 + N_BAND)
            
            # Simplified MA Signal: Score increases if the latest price is above the upper band.
            if latest_prices[ticker] > upper:
                 score += 1
        
        today_scores[ticker] = score

    # 4. Determine Final Weights
    scalars = today_scores.map(SCALAR_MAP)
    invested_weights = scalars * pd.Series(BASE_WEIGHTS)
    
    # Format results
    result_weights = invested_weights.to_dict()
    cash_weight = 1.0 - invested_weights.sum()
    result_weights['Cash'] = cash_weight
    
    # 5. Calculate Previous Day's Strategy Return (for the report)
    if len(prices_df) >= 2:
        yesterday_asset_returns = prices_df.iloc[-1] / prices_df.iloc[-2] - 1
        daily_return = (invested_weights * yesterday_asset_returns).sum()
    else:
        daily_return = 0.0

    return result_weights, f"Previous Day's Strategy Return: {daily_return:.2%}"

# --- Telegram Transmission and Scheduling Logic ---

def send_telegram_message(message):
    """Function to send the message via Telegram bot"""
    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n==================================================")
        print("Warning: TELEGRAM_TOKEN or CHAT_ID is NOT configured.")
        print("==================================================")
        print(f"\n[Report Content Preview]\n{message}")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        print("Telegram message sent successfully!")
    except requests.exceptions.RequestException as e:
        print(f"Telegram message sending failed: {e}")

def get_target_date():
    """Determines the base date for data analysis."""
    today = datetime.date.today()
    
    # ★★★ TEST_MODE 로직: 환경 변수가 TRUE면 주말 체크를 무시함 ★★★
    if os.environ.get('TEST_MODE') == 'TRUE':
        # 테스트 모드에서는 지난 금요일(가장 최근의 유효 데이터)을 기준으로 분석 시도
        if today.weekday() in [5, 6]:
            days_to_subtract = today.weekday() - 4
            test_date = today - datetime.timedelta(days=days_to_subtract)
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] TEST MODE ON: Forcing analysis based on {test_date.strftime('%Y-%m-%d')}")
            return test_date
        # 주중 테스트 실행 시 어제 날짜 사용
        return today - datetime.timedelta(days=1)


    if today.weekday() == 0:  # Monday (0) -> Use last Friday's closing price
        return today - datetime.timedelta(days=3)
    elif today.weekday() in [5, 6]:  # Saturday (5), Sunday (6) -> Do not send
        return None
    else:  # Tuesday to Friday -> Use yesterday's closing price
        return today - datetime.timedelta(days=1)

def format_report(target_date, weights, daily_return_info):
    """Formats the report message in Markdown."""
    
    # MDD, CAGR values are fixed for the report (using backtest results)
    CAGR_VALUE = "16.31%"
    MDD_VALUE = "-3.34%"

    # Sort weights by size (descending)
    sorted_weights = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    
    report_lines = [
        f"🌟 **MA Individual Strategy Daily Report ({target_date.strftime('%Y년 %m월 %d일 기준')})**",
        "---------------------------------------------------",
        "✅ **Strategy Overview:** 5 Assets (3 Stocks + 2 Bonds) with individual weight adjustment based on 20/120/200-day MA trend signals (Includes Cash-Out)",
        f"📅 **{daily_return_info}**",
        "",
        "### 💰 Today's Portfolio Weights (Max 100%)",
        "| Asset Name | Investment Weight |",
        "| :--- | :--- |"
    ]
    
    for ticker, weight in sorted_weights:
        name = TICKER_NAMES.get(ticker, ticker)
        report_lines.append(f"| {name} | **{weight:.2%}** |")
        
    report_lines.append("---------------------------------------------------")
    report_lines.append(f"⚠️ **Note:** MDD {MDD_VALUE}, CAGR {CAGR_VALUE} (Based on 2024 Mar ~ 2025 Nov Backtest)")
    
    return "\n".join(report_lines)

if __name__ == "__main__":
    
    # Record execution time
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Auto Report execution started.")
    
    target_date = get_target_date()
    
    if target_date is None:
        print("오늘은 주말(토/일)이므로 보고서를 발송하지 않습니다.")
        sys.exit(0)
    
    # 1. Execute MA Strategy and calculate final weights
    weights, daily_return_info = run_ma_strategy_for_date(target_date)
    
    if weights is None:
        report = f"❌ **MA Individual Strategy Report - Failed**\nBase Date: {target_date.strftime('%Y-%m-%d')}\nReason: {daily_return_info}"
    else:
        # 2. Format the report
        report = format_report(target_date, weights, daily_return_info)
    
    # 3. Send Telegram
    send_telegram_message(report)
