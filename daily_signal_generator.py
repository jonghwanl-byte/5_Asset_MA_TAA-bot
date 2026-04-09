import yfinance as yf
import numpy as np
import pandas as pd
import sys
import os
import requests
from datetime import datetime
import pytz
import time

# --- [1. 전략 파라미터 설정] ---

# 한글 이름과 티커 매핑
ASSET_NAMES = [
    '한국 주식', # 102110.KS
    '중국 주식', # 283580.KS
    '일본 주식', # 241180.KS
    '인도 주식', # 453810.KS
    '채권 30년', # 385560.KS
    '채권 10년', # 148070.KS
]
TICKER_MAP = {
    '한국 주식': '102110.KS',
    '중국 주식': '283580.KS',
    '일본 주식': '241180.KS', # [수정 완료] 콜론(:) 확인
    '인도 주식': '453810.KS',
    '채권 30년': '385560.KS',
    '채권 10년': '148070.KS'
}
TICKER_LIST = list(TICKER_MAP.values())

# 기본 설정
BASE_WEIGHTS = {name: 0.20 for name in ASSET_NAMES} # 20% 균등 배분
MA_WINDOWS = [20, 120, 200]
N_BAND = 0.03 # 3% 이격도
SCALAR_MAP = {3: 1.0, 2: 0.75, 1: 0.50, 0: 0.0} # 시나리오 A

# 텔레그램 Secrets
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')

# --- [2. 텔레그램 전송 함수] ---
# [수정] parse_mode를 HTML로 설정하여 특수문자 충돌 방지
def send_telegram_message(token, chat_id, message, parse_mode='HTML'):
    if not token or not chat_id:
        print("텔레그램 TOKEN 또는 CHAT_ID가 설정되지 않았습니다.", file=sys.stderr)
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    # [수정] json 대신 data를 사용하여 전송 안정성 강화
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': parse_mode}
    try:
        response = requests.post(url, data=payload, timeout=15)
        if response.status_code != 200:
            print(f"텔레그램 응답 에러: {response.text}", file=sys.stderr)
        response.raise_for_status()
        print("텔레그램 메시지 전송 성공.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
        return False

# --- [3. 일일 신호 계산 함수] ---
def get_daily_signals_and_report():
    
    print("... 최신 시장 데이터 다운로드 중 ...")
    data_full = yf.download(TICKER_LIST, period="400d", progress=False)
    
    if data_full.empty:
        raise ValueError("데이터 다운로드에 실패했습니다.")
    
    all_prices_df_raw = data_full['Close'].ffill()
    all_prices_df = all_prices_df_raw.rename(columns={v: k for k, v in TICKER_MAP.items()})
    
    # --- [4. 이격도(Hysteresis) 상태 계산] ---
    ma_lines = {}
    upper_bands = {}
    lower_bands = {}
    
    for name in ASSET_NAMES:
        for window in MA_WINDOWS:
            ma_key = f"{name}_{window}"
            ma_lines[ma_key] = all_prices_df[name].rolling(window=window).mean()
            upper_bands[ma_key] = ma_lines[ma_key] * (1.0 + N_BAND)
            lower_bands[ma_key] = ma_lines[ma_key] * (1.0 - N_BAND)

    yesterday_ma_states = {f"{name}_{window}": 0.0 for name in ASSET_NAMES for window in MA_WINDOWS}
    today_scalars = pd.Series(0.0, index=ASSET_NAMES)
    yesterday_scalars = pd.Series(0.0, index=ASSET_NAMES)
    
    today_ma_states_dict = yesterday_ma_states.copy()
    yesterday_ma_states_dict = yesterday_ma_states.copy()

    start_index = max(MA_WINDOWS) - 1 
    
    for i in range(start_index, len(all_prices_df)):
        today_scores = pd.Series(0, index=ASSET_NAMES)
        current_ma_states = {}
        
        for name in ASSET_NAMES:
            score = 0
            for window in MA_WINDOWS:
                ma_key = f"{name}_{window}"
                yesterday_state = yesterday_ma_states[ma_key]
                
                price = all_prices_df[name].iloc[i]
                upper = upper_bands[ma_key].iloc[i]
                lower = lower_bands[ma_key].iloc[i]
                
                if pd.isna(upper): new_state = 0.0
                elif yesterday_state == 1.0: 
                    new_state = 1.0 if price >= lower else 0.0
                else: 
                    new_state = 1.0 if price > upper else 0.0
                
                current_ma_states[ma_key] = new_state
                score += new_state
            
            today_scores[name] = score
        
        if i == len(all_prices_df) - 2:
            yesterday_scalars = today_scores.map(SCALAR_MAP)
            yesterday_ma_states_dict = current_ma_states
        if i == len(all_prices_df) - 1:
            today_scalars = today_scores.map(SCALAR_MAP)
            today_ma_states_dict = current_ma_states
        
        yesterday_ma_states = current_ma_states

    # --- [5. 최종 비중 계산] ---
    today_weights = (today_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    yesterday_weights = (yesterday_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    
    today_total_cash = 1.0 - sum(today_weights.values())
    yesterday_total_cash = 1.0 - sum(yesterday_weights.values())
    
    is_rebalancing_needed = not (today_scalars.equals(yesterday_scalars))
    
    # --- [6. 리포트 작성 (HTML 태그 적용)] ---
    yesterday = all_prices_df.index[-1]
    kst = pytz.timezone('Asia/Seoul')
    yesterday_kst = yesterday.astimezone(kst) if yesterday.tzinfo else kst.localize(yesterday)
    
    report = []
    # [수정] **굵게** 대신 <b>굵게</b> 사용
    report.append(f"🔔 <b>TAA Bot - 5 Asset (Hysteresis 3%)</b>")
    report.append(f"({yesterday_kst.strftime('%Y-%m-%d %A')} 마감 기준)")

    if is_rebalancing_needed:
        report.append("\n🔼 <b>리밸런싱: 매매 필요</b>")
        report.append("(목표 비중이 변경되었습니다)")
    else:
        report.append("\n🟢 <b>리밸런싱: 매매 불필요</b>")
        report.append("(비중 유지)")
    
    report.append("\n" + "-"*20)
    report.append("💰 <b>[1] 오늘 목표 비중</b>")
    
    for name in ASSET_NAMES:
        emoji = "🎯" if today_weights[name] != yesterday_weights[name] else "*"
        report.append(f"{emoji} {name}: {today_weights[name]:.1%}")
    
    cash_emoji = "🎯" if abs(today_total_cash - yesterday_total_cash) > 0.0001 else "*"
    report.append(f"{cash_emoji} 현금 (Cash): {today_total_cash:.1%}")
    
    report.append("\n" + "-"*20)
    report.append("📊 <b>[2] 비중 변경 상세</b>")
    
    def format_change_row(name, yesterday, today):
        delta = today - yesterday
        change_str = "(유지)" if abs(delta) < 0.0001 else f"{'🔼' if delta > 0 else '🔽'} {delta:+.1%}"
        return f"{name.ljust(9)}: {yesterday:.1%}".rjust(7) + f" -> {today:.1%}".rjust(7) + f" | {change_str.rjust(10)}"

    for name in ASSET_NAMES:
        report.append(format_change_row(name, yesterday_weights[name], today_weights[name]))
    report.append(format_change_row('현금', yesterday_total_cash, today_total_cash))
    
    report.append("\n" + "-"*20)
    report.append("📈 <b>[3] 전일 시장 현황</b>")
    today_prices = all_prices_df.iloc[-1]
    price_change = all_prices_df.pct_change().iloc[-1]
    
    for name in ASSET_NAMES:
        emoji = "🔴" if price_change[name] >= 0 else "🔵"
        report.append(f"{emoji} {name}: {today_prices[name]:,.0f} ({price_change[name]:+.1%})")
    
    report.append("\n" + "-"*20)
    report.append("🔍 <b>[4] MA 신호 상세</b>")
    report.append(f"(이격도 +/- {N_BAND:.1%} 룰)")
    
    for name in ASSET_NAMES:
        score = int(today_scalars[name] * 3 / 1.0) if today_scalars[name] > 0 else 0
        status_emoji = "🟢ON" if score > 0 else "🔴OFF"
        report.append(f"\n<b>{name} ({score}/3 {status_emoji})</b>")
        
        for window in MA_WINDOWS:
            ma_key = f"{name}_{window}"
            state_emoji = "ON" if today_ma_states_dict[ma_key] == 1.0 else "OFF"
