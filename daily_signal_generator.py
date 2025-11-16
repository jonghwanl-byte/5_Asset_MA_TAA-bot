import yfinance as yf
import numpy as np
import pandas as pd
import sys
import os
import requests
from datetime import datetime
import pytz
import time # [수정] time 라이브러리 임포트 추가

# --- [1. 전략 파라미터 설정] ---
ASSETS = ['102110.KS', '283580.KS', '453810.KS', '148070.KS', '385560.KS']
BASE_WEIGHTS = {ticker: 0.20 for ticker in ASSETS} # 20% 균등 배분
MA_WINDOWS = [20, 120, 200]
SCALAR_MAP = {3: 1.0, 2: 0.75, 1: 0.50, 0: 0.0} # 시나리오 A

# 텔레그램 Secrets (환경 변수에서 로드)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')

# --- [2. 텔레그램 전송 함수] ---
def send_telegram_message(token, chat_id, message, parse_mode='Markdown'):
    """텔레그램으로 메시지를 전송합니다."""
    if not token or not chat_id:
        print("텔레그램 TOKEN 또는 CHAT_ID가 설정되지 않았습니다. Secrets를 확인하세요.", file=sys.stderr)
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': parse_mode
    }
    try:
        response = requests.post(url, json=payload, timeout=10) # 10초 타임아웃
        response.raise_for_status() 
        print("텔레그램 메시지 전송 성공.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"텔레그램 전송 실패: {e}\n응답: {e.response.text}", file=sys.stderr)
        return False

# --- [3. 일일 신호 계산 함수] ---
def get_daily_signals_and_report():
    
    print("... 최신 시장 데이터 다운로드 중 ...")
    data_full = yf.download(ASSETS, period="400d", progress=False)
    
    if data_full.empty:
        raise ValueError("데이터 다운로드에 실패했습니다.")
    
    all_prices_df = data_full['Close'].ffill()
    
    # --- [4. MA 및 신호 계산 (Hysteresis 없음)] ---
    
    sig_20 = (all_prices_df > all_prices_df.rolling(window=20).mean()).astype(int)
    sig_120 = (all_prices_df > all_prices_df.rolling(window=120).mean()).astype(int)
    sig_200 = (all_prices_df > all_prices_df.rolling(window=200).mean()).astype(int)
    
    total_scores = (sig_20 + sig_120 + sig_200)
    
    scalars = total_scores.map(lambda x: SCALAR_MAP.get(x, 0.0))
    
    today_scalars = scalars.iloc[-1]
    yesterday_scalars = scalars.iloc[-2]
    
    today_prices = all_prices_df.iloc[-1]
    price_change = all_prices_df.pct_change().iloc[-1]

    # --- [5. 최종 비중 계산] ---
    today_weights = (today_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    yesterday_weights = (yesterday_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    
    today_total_cash = 1.0 - sum(today_weights.values())
    yesterday_total_cash = 1.0 - sum(yesterday_weights.values())
    
    is_rebalancing_needed = not (today_scalars.equals(yesterday_scalars))
    
    # --- [6. 알림 메시지 생성] ---
    
    yesterday = all_prices_df.index[-1]
    kst = pytz.timezone('Asia/Seoul')
    
    if yesterday.tzinfo is None:
        yesterday_kst = kst.localize(yesterday)
    else:
        yesterday_kst = yesterday.astimezone(kst)
    
    # --- [메시지 1: 핵심 요약] ---
    report_summary = []
    report_summary.append(f"🔔 TAA Bot - 5 Asset MA Strategy")
    report_summary.append(f"({yesterday_kst.strftime('%Y-%m-%d %A')} 마감 기준)")

    # [1] 리밸런싱 신호
    if is_rebalancing_needed:
        report_summary.append("\n" + "🔼 ====================== 🔼")
        report_summary.append("    리밸런싱 신호: \"매매 필요\"")
        report_summary.append("🔼 ====================== 🔼")
        report_summary.append("(MA 신호 변경으로 목표 비중이 어제와 다릅니다)")
    else:
        report_summary.append("\n" + "🟢 ====================== 🟢")
        report_summary.append("    리밸런싱 신호: \"매매 불필요\"")
        report_summary.append("🟢 ====================== 🟢")
        report_summary.append("(모든 MA 신호가 어제와 동일하게 유지되었습니다)")
    
    report_summary.append("\n" + "---")

    # [2] 오늘 목표 비중
    report_summary.append("💰 [1] 오늘 목표 비중 (신규)")
    
    for ticker in ASSETS:
        emoji = "🎯" if today_weights[ticker] != yesterday_weights[ticker] else "*"
        report_summary.append(f" {emoji} {ticker}: {today_weights[ticker]:.1%}")
    
    cash_emoji = "🎯" if abs(today_total_cash - yesterday_total_cash) > 0.0001 else "*"
    report_summary.append(f" {cash_emoji} 현금 (Cash): {today_total_cash:.1%}")
    
    report_summary.append("\n" + "---")
    
    # [3] 비중 변경 상세 (Monospace)
    report_summary.append("📊 [2] 비중 변경 상세 (매매 신호)")
    report_summary.append("```") # Monospace 시작
    report_summary.append("자산        (어제)   (오늘)  | (변경폭)")
    report_summary.append("---------------------------------------")

    def format_change_row(ticker, yesterday, today):
        delta = today - yesterday
        if abs(delta) < 0.0001:
            change_str = "(유지)"
        else:
            emoji = "🔼" if delta > 0 else "🔽"
            change_str = f"{emoji} {delta:+.1%}"
        
        ticker_str = ticker.ljust(10)
        yesterday_str = f"{yesterday:.1%}".rjust(7)
        today_str = f"{today:.1%}".rjust(7)
        change_str = change_str.rjust(10)

        return f"{ticker_str}: {yesterday_str} -> {today_str} | {change_str}"

    for ticker in ASSETS:
        report_summary.append(format_change_row(ticker, yesterday_weights[ticker], today_weights[ticker]))
    
    report_summary.append(format_change_row('현금', yesterday_total_cash, today_total_cash))
    report_summary.append("---------------------------------------")
    report_summary.append("```") # Monospace 끝
    
    # --- [메시지 2: 상세 정보] ---
    report_detail = []
    report_detail.append(f"--- (상세 정보: {yesterday_kst.strftime('%Y-%m-%d')}) ---")
    
    # [4] 전일 시장 현황
    report_detail.append("\n" + "📈 [3] 전일 시장 현황")
    
    def format_price_line(ticker_name, price, change):
        emoji = "🔴" if change >= 0 else "🔵"
        return f"{emoji} {ticker_name}: {price:.1f} ({change:+.1%})"
        
    for ticker in ASSETS:
        report_detail.append(f"{format_price_line(ticker, today_prices[ticker], price_change[ticker])}")
    
    report_detail.append("\n" + "---")
    
    # [5] MA 신호 상세
    report_detail.append("🔍 [4] MA 신호 상세 (오늘 기준)")
    report_detail.append(f"(단순 돌파 룰 적용)")
    
    for ticker in ASSETS:
        score = total_scores[ticker].iloc[-1]
        status_emoji = "🟢ON" if score > 0 else "🔴OFF"
        
        report_detail.append(f"\n**{ticker} (신호: {score}/3개 {status_emoji})**")
        
        for window in MA_WINDOWS:
            sig_df = locals()[f'sig_{window}']
            
            today_state_val = sig_df[ticker].iloc[-1]
            yesterday_state_val = sig_df[ticker].iloc[-2]
            
            state_emoji = "🟢ON" if today_state_val == 1.0 else "🔴OFF"
            
            if today_state_val > yesterday_state_val: state_change = "[신규 ON]"
            elif today_state_val < yesterday_state_val: state_change = "[신규 OFF]"
            else: state_change = "[유지]"
            
            t_price = today_prices[ticker]
            ma_val = all_prices_df[ticker].rolling(window=window).mean().iloc[-1]
            
            if pd.isna(ma_val):
                disparity = 0.0
            else:
                disparity = (t_price / ma_val) - 1.0
            
            report_detail.append(f"* {window}일: {state_emoji} (이격도: {disparity:+.1%}) {state_change}")
    
    return "\n".join(report_summary), "\n".join(report_detail)

# --- [5. 메인 실행] ---
if __name__ == "__main__":
        
    try:
        # 1. 리포트 생성 (2개로 분할)
        report_summary, report_detail = get_daily_signals_and_report()
        
        # 2. 터미널에 출력 (GitHub Actions 로그용)
        print("--- [생성된 리포트 1] ---")
        print(report_summary)
        print("--- [생성된 리포트 2] ---")
        print(report_detail)
        print("---------------------")
        
        # 3. 텔레그램으로 전송 (2개 메시지 순차 전송)
        success1 = send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, report_summary)
        # 텔레그램 API 과부하 방지를 위해 1초 대기
        time.sleep(1) # [수정] 이 코드를 위해 import time 이 필요합니다.
        success2 = send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, report_detail)
        
        if success1 and success2:
            print("2개 메시지 전송 완료.")
        else:
            raise Exception("텔레그램 전송에 실패했습니다. 로그를 확인하세요.")
        
    except Exception as e:
        print(f"전략 실행 중 오류가 발생했습니다: {e}", file=sys.stderr)
        
        kst = pytz.timezone('Asia/Seoul')
        error_message = f"🚨 TAA Bot 실행 실패 🚨\n({datetime.now(kst).strftime('%Y-%m-%d %H:%M')})\n\n오류:\n{e}"
        
        # 오류 메시지는 Markdown 서식을 '제외'하고 순수 텍스트(Plain Text)로 전송
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, error_message, parse_mode='None')
        sys.exit(1)
