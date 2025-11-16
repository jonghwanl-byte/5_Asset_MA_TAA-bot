import yfinance as yf
import numpy as np
import pandas as pd
import sys
import os
import requests
from datetime import datetime
import pytz

# --- [1. 전략 파라미터 설정] ---
ASSETS = ['102110.KS', '283580.KS', '453810.KS', '148070.KS', '385560.KS']
BASE_WEIGHTS = {ticker: 0.20 for ticker in ASSETS} # 20% 균등 배분
MA_WINDOWS = [20, 120, 200]
SCALAR_MAP = {3: 1.0, 2: 0.75, 1: 0.50, 0: 0.0} # 시나리오 A

# 텔레그램 Secrets (환경 변수에서 로드)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')

# --- [2. 텔레그램 전송 함수] ---
def send_telegram_message(token, chat_id, message):
    """텔레그램으로 메시지를 전송합니다."""
    if not token or not chat_id:
        print("텔레그램 TOKEN 또는 CHAT_ID가 설정되지 않았습니다. Secrets를 확인하세요.", file=sys.stderr)
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'Markdown' # 텔레그램 서식(고정폭)을 위해 Markdown 사용
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status() # 오류가 있으면 예외 발생
        print("텔레그램 메시지 전송 성공.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"텔레그램 전송 실패: {e}\n응답: {e.response.text}", file=sys.stderr)
        return False

# --- [3. 일일 신호 계산 함수] ---
def get_daily_signals_and_report():
    
    print("... 최신 시장 데이터 다운로드 중 ...")
    # MA 계산을 위해 최소 200일 + 200일(버퍼) 데이터 다운로드
    data_full = yf.download(ASSETS, period="400d", progress=False)
    
    if data_full.empty:
        raise ValueError("데이터 다운로드에 실패했습니다.")
    
    all_prices_df = data_full['Close'].ffill()
    
    # --- [4. MA 및 신호 계산 (Hysteresis 없음)] ---
    
    # 각 MA별 신호 (1=ON, 0=OFF)
    sig_20 = (all_prices_df > all_prices_df.rolling(window=20).mean()).astype(int)
    sig_120 = (all_prices_df > all_prices_df.rolling(window=120).mean()).astype(int)
    sig_200 = (all_prices_df > all_prices_df.rolling(window=200).mean()).astype(int)
    
    # 총 점수 (0~3점)
    total_scores = (sig_20 + sig_120 + sig_200)
    
    # 시나리오 A 스케일러 적용 (0.0, 0.5, 0.75, 1.0)
    scalars = total_scores.map(SCALAR_MAP)
    
    # '오늘' (어제 마감) / '어제' (그제 마감) 데이터 추출
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
    yesterday_kst = yesterday.astimezone(kst) # KST로 변환
    
    report = []
    report.append(f"🔔 TAA Bot - 5 Asset MA Strategy")
    report.append(f"({yesterday_kst.strftime('%Y-%m-%d %A')} 마감 기준)") # KST 기준 날짜/요일

    # [1] 리밸런싱 신호
    if is_rebalancing_needed:
        report.append("\n" + "🔼 ====================== 🔼")
        report.append("    리밸런싱 신호: \"매매 필요\"")
        report.append("🔼 ====================== 🔼")
        report.append("(MA 신호 변경으로 목표 비중이 어제와 다릅니다)")
    else:
        report.append("\n" + "🟢 ====================== 🟢")
        report.append("    리밸런싱 신호: \"매매 불필요\"")
        report.append("🟢 ====================== 🟢")
        report.append("(모든 MA 신호가 어제와 동일하게 유지되었습니다)")
    
    report.append("\n" + "---")

    # [2] 오늘 목표 비중
    report.append("💰 [1] 오늘 목표 비중 (신규)")
    
    for ticker in ASSETS:
        emoji = "🎯" if today_weights[ticker] != yesterday_weights[ticker] else "*"
        report.append(f" {emoji} {ticker}: {today_weights[ticker]:.1%}")
    
    cash_emoji = "🎯" if abs(today_total_cash - yesterday_total_cash) > 0.0001 else "*"
    report.append(f" {cash_emoji} 현금 (Cash): {today_total_cash:.1%}")
    
    report.append("\n" + "---")
    
    # [3] 비중 변경 상세 (Monospace)
    report.append("📊 [2] 비중 변경 상세 (매매 신호)")
    report.append("```") # Monospace 시작
    report.append("자산        (어제)   (오늘)  | (변경폭)")
    report.append("---------------------------------------")

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
        report.append(format_change_row(ticker, yesterday_weights[ticker], today_weights[ticker]))
    
    report.append(format_change_row('현금', yesterday_total_cash, today_total_cash))
    report.append("---------------------------------------")
    report.append("```") # Monospace 끝
    
    report.append("\n" + "---")
    
    # [4. 전일 시장 현황]
    report.append("📈 [3] 전일 시장 현황")
    
    def format_price_line(ticker_name, price, change):
        emoji = "🔴" if change >= 0 else "🔵"
        return f"{emoji} {ticker_name}: {price:.1f} ({change:+.1%})"
        
    for ticker in ASSETS:
        report.append(f"{format_price_line(ticker, today_prices[ticker], price_change[ticker])}")
    
    report.append("\n" + "---")
    
    # [5] MA 신호 상세
    report.append("🔍 [4] MA 신호 상세 (오늘 기준)")
    report.append(f"(단순 돌파 룰 적용)")
    
    for ticker in ASSETS:
        score = total_scores[ticker].iloc[-1]
        status_emoji = "🟢ON" if score > 0 else "🔴OFF"
        
        report.append(f"\n**{ticker} (신호: {score}/3개 {status_emoji})**")
        
        # 20, 120, 200일선 신호 상세
        for window in MA_WINDOWS:
            sig_df = locals()[f'sig_{window}'] # sig_20, sig_120, sig_200
            
            today_state_val = sig_df[ticker].iloc[-1]
            yesterday_state_val = sig_df[ticker].iloc[-2]
            
            state_emoji = "🟢ON" if today_state_val == 1.0 else "🔴OFF"
            
            if today_state_val > yesterday_state_val: state_change = "[신규 ON]"
            elif today_state_val < yesterday_state_val: state_change = "[신규 OFF]"
            else: state_change = "[유지]"
            
            t_price = today_prices[ticker]
            ma_val = all_prices_df[ticker].rolling(window=window).mean().iloc[-1]
            disparity = (t_price / ma_val) - 1.0
            
            report.append(f"* {window}일: {state_emoji} (이격도: {disparity:+.1%}) {state_change}")
    
    return "\n".join(report)

# --- [5. 메인 실행] ---
if __name__ == "__main__":
    
    # --- [수정] ---
    # 0. 주말/휴일 확인 로직 '제거'
    # if check_weekend():
    #     sys.exit(0) 
    # -------------
        
    try:
        # 1. 리포트 생성
        daily_report = get_daily_signals_and_report()
        
        # 2. 터미널에 출력 (GitHub Actions 로그용)
        print("--- [생성된 리포트] ---")
        print(daily_report)
        print("---------------------")
        
        # 3. 텔레그램으로 전송
        if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, daily_report):
            print("전송 완료.")
        else:
            raise Exception("텔레그램 전송에 실패했습니다. 로그를 확인하세요.")
        
    except Exception as e:
        print(f"전략 실행 중 오류가 발생했습니다: {e}", file=sys.stderr)
        # 텔레그램으로 오류 메시지 전송 시도
        kst = pytz.timezone('Asia/Seoul')
        error_message = f"🚨 TAA Bot 실행 실패 🚨\n({datetime.now(kst).strftime('%Y-%m-%d %H:%M')})\n\n오류: {e}"
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, error_message)
        sys.exit(1)
