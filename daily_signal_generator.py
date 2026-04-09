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
ASSET_NAMES = ['한국 주식', '중국 주식', '일본 주식', '인도 주식', '채권 30년', '채권 10년']
TICKER_MAP = {
    '한국 주식': '102110.KS', '중국 주식': '283580.KS', '일본 주식': '241180.KS',
    '인도 주식': '453810.KS', '채권 30년': '385560.KS', '채권 10년': '148070.KS'
}
TICKER_LIST = list(TICKER_MAP.values())
BASE_WEIGHTS = {name: 0.20 for name in ASSET_NAMES}
MA_WINDOWS = [20, 120, 200]
N_BAND = 0.03
SCALAR_MAP = {3: 1.0, 2: 0.75, 1: 0.50, 0: 0.0}

# 텔레그램 Secrets 불러오기
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')

# --- [2. 디버깅 강화된 전송 함수] ---
def send_telegram_message(token, chat_id, message, parse_mode='HTML'):
    print("\n--- [텔레그램 전송 디버깅 정보] ---")
    
    # 1. 시크릿 설정 여부 확인
    if not token:
        print("❌ 오류: TELEGRAM_TOKEN이 비어있습니다. GitHub Secrets를 확인하세요.")
        return False
    if not chat_id:
        print("❌ 오류: TELEGRAM_TO(CHAT_ID)가 비어있습니다. GitHub Secrets를 확인하세요.")
        return False

    # 2. 값의 형식만 살짝 출력 (보안상 마스킹)
    print(f"입력된 CHAT_ID: {chat_id}")
    print(f"TOKEN 길이: {len(token)}자")
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': parse_mode}
    
    try:
        print("📡 텔레그램 서버로 요청을 보냅니다...")
        response = requests.post(url, data=payload, timeout=15)
        
        # 3. 상세 응답 로그 확인
        if response.status_code == 200:
            res_json = response.json()
            print(f"✅ 전송 성공! (메시지 ID: {res_json.get('result', {}).get('message_id')})")
            return True
        else:
            print(f"❌ 전송 실패! 상태 코드: {response.status_code}")
            print(f"🗣️ 서버 응답: {response.text}")
            return False
            
    except Exception as e:
        print(f"⚠️ 네트워크 오류 발생: {e}")
        return False

# --- [3. 데이터 계산 및 리포트 생성 (기존과 동일)] ---
def get_daily_signals_and_report():
    print("... 최신 시장 데이터 다운로드 중 ...")
    data_full = yf.download(TICKER_LIST, period="400d", progress=False)
    if data_full.empty: raise ValueError("데이터 다운로드에 실패했습니다.")
    
    all_prices_df_raw = data_full['Close'].ffill()
    all_prices_df = all_prices_df_raw.rename(columns={v: k for k, v in TICKER_MAP.items()})
    
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
                price, upper, lower = all_prices_df[name].iloc[i], upper_bands[ma_key].iloc[i], lower_bands[ma_key].iloc[i]
                if pd.isna(upper): new_state = 0.0
                elif yesterday_ma_states[ma_key] == 1.0: new_state = 1.0 if price >= lower else 0.0
                else: new_state = 1.0 if price > upper else 0.0
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

    today_weights = (today_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    yesterday_weights = (yesterday_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    today_total_cash = 1.0 - sum(today_weights.values())
    yesterday_total_cash = 1.0 - sum(yesterday_weights.values())
    is_rebalancing_needed = not (today_scalars.equals(yesterday_scalars))
    
    yesterday = all_prices_df.index[-1]
    kst = pytz.timezone('Asia/Seoul')
    yesterday_kst = yesterday.astimezone(kst) if yesterday.tzinfo else kst.localize(yesterday)
    
    report = [f"🔔 <b>TAA Bot - 5 Asset (Hysteresis 3%)</b>", f"({yesterday_kst.strftime('%Y-%m-%d %A')} 마감 기준)"]
    report.append("\n🔼 <b>리밸런싱: 필요</b>" if is_rebalancing_needed else "\n🟢 <b>리밸런싱: 불필요</b>")
    report.append("-" * 20 + "\n💰 <b>[1] 목표 비중</b>")
    for name in ASSET_NAMES: report.append(f"{'🎯' if today_weights[name] != yesterday_weights[name] else '*'} {name}: {today_weights[name]:.1%}")
    report.append(f"* 현금 (Cash): {today_total_cash:.1%}\n" + "-" * 20 + "\n📈 <b>[2] 시장 현황</b>")
    today_prices, price_change = all_prices_df.iloc[-1], all_prices_df.pct_change().iloc[-1]
    for name in ASSET_NAMES: report.append(f"{'🔴' if price_change[name] >= 0 else '🔵'} {name}: {today_prices[name]:,.0f} ({price_change[name]:+.1%})")
    
    return "\n".join(report)

# --- [4. 메인 실행] ---
if __name__ == "__main__":
    try:
        pd.set_option('display.width', 1000)
        full_report = get_daily_signals_and_report()
        
        # 전송 시도
        if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, full_report):
            print("\n✅ 모든 프로세스가 성공적으로 완료되었습니다.")
        else:
            print("\n❌ 전송에 실패했습니다. 로그 내용을 확인하세요.")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n🚨 실행 중 오류 발생: {e}")
        sys.exit(1)
