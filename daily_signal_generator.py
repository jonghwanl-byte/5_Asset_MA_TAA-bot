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

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')

# --- [2. 텔레그램 전송 함수] ---
def send_telegram_message(token, chat_id, message, parse_mode='HTML'):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': parse_mode}
    try:
        response = requests.post(url, data=payload, timeout=15)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
        return False

# --- [3. 일일 신호 계산 및 '상세' 리포트 생성] ---
def get_daily_signals_and_report():
    print("... 최신 시장 데이터 다운로드 중 ...")
    data_full = yf.download(TICKER_LIST, period="400d", progress=False)
    if data_full.empty: raise ValueError("데이터 다운로드에 실패했습니다.")
    
    all_prices_df_raw = data_full['Close'].ffill()
    all_prices_df = all_prices_df_raw.rename(columns={v: k for k, v in TICKER_MAP.items()})
    
    # 이격도 및 이동평균 계산
    ma_lines = {f"{n}_{w}": all_prices_df[n].rolling(window=w).mean() for n in ASSET_NAMES for w in MA_WINDOWS}
    upper_bands = {k: v * (1.0 + N_BAND) for k, v in ma_lines.items()}
    lower_bands = {k: v * (1.0 - N_BAND) for k, v in ma_lines.items()}

    yesterday_ma_states = {f"{n}_{w}": 0.0 for n in ASSET_NAMES for w in MA_WINDOWS}
    yesterday_ma_states_dict = {}
    today_ma_states_dict = {}

    for i in range(max(MA_WINDOWS)-1, len(all_prices_df)):
        today_scores = pd.Series(0, index=ASSET_NAMES)
        current_ma_states = {}
        for name in ASSET_NAMES:
            score = 0
            for window in MA_WINDOWS:
                k = f"{name}_{window}"
                p, u, l = all_prices_df[name].iloc[i], upper_bands[k].iloc[i], lower_bands[k].iloc[i]
                new_state = (1.0 if p >= l else 0.0) if yesterday_ma_states[k] == 1.0 else (1.0 if p > u else 0.0)
                current_ma_states[k] = new_state
                score += new_state
            today_scores[name] = score
        
        if i == len(all_prices_df) - 2:
            yesterday_scalars = today_scores.map(SCALAR_MAP)
            yesterday_ma_states_dict = current_ma_states
        if i == len(all_prices_df) - 1:
            today_scalars = today_scores.map(SCALAR_MAP)
            today_ma_states_dict = current_ma_states
        yesterday_ma_states = current_ma_states

    # 비중 계산
    today_w = (today_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    yesterday_w = (yesterday_scalars * pd.Series(BASE_WEIGHTS)).to_dict()
    t_cash, y_cash = 1.0 - sum(today_w.values()), 1.0 - sum(yesterday_w.values())
    is_rebalancing = not today_scalars.equals(yesterday_scalars)
    
    # 리포트 조립 시작
    kst = pytz.timezone('Asia/Seoul')
    dt_str = all_prices_df.index[-1].astimezone(kst).strftime('%Y-%m-%d %A') if all_prices_df.index[-1].tzinfo else kst.localize(all_prices_df.index[-1]).strftime('%Y-%m-%d %A')
    
    report = [f"🔔 <b>TAA Bot - 5 Asset (Hysteresis 3%)</b>", f"({dt_str} 마감 기준)"]
    report.append("\n🔼 <b>리밸런싱: 필요</b>" if is_rebalancing else "\n🟢 <b>리밸런싱: 불필요</b>")
    report.append("-" * 20 + "\n💰 <b>[1] 오늘 목표 비중</b>")
    for n in ASSET_NAMES: report.append(f"{'🎯' if today_w[n] != yesterday_w[n] else '*'} {n}: {today_w[n]:.1%}")
    report.append(f"{'🎯' if abs(t_cash-y_cash)>0.001 else '*'} 현금 (Cash): {t_cash:.1%}")

    report.append("\n" + "-" * 20 + "\n📊 <b>[2] 비중 변경 상세</b>")
    for n in ASSET_NAMES + ['현금']:
        tw, yw = (today_w.get(n, t_cash), yesterday_w.get(n, y_cash))
        diff = tw - yw
        change = "(유지)" if abs(diff) < 0.001 else f"{'🔼' if diff > 0 else '🔽'} {diff:+.1%}"
        report.append(f"{n.ljust(8)}: {yw:>5.1%} → {tw:>5.1%} | {change}")

    report.append("\n" + "-" * 20 + "\n📈 <b>[3] 전일 시장 현황</b>")
    prices, changes = all_prices_df.iloc[-1], all_prices_df.pct_change().iloc[-1]
    for n in ASSET_NAMES: report.append(f"{'🔴' if changes[n] >= 0 else '🔵'} {n}: {prices[n]:,.0f} ({changes[n]:+.1%})")

    report.append("\n" + "-" * 20 + "\n🔍 <b>[4] MA 신호 상세</b>")
    for n in ASSET_NAMES:
        score = int(today_scalars[n] * 3 / 1.0) if today_scalars[n] > 0 else 0
        report.append(f"\n<b>{n} ({score}/3 {'🟢ON' if score > 0 else '🔴OFF'})</b>")
        for w in MA_WINDOWS:
            k = f"{n}_{w}"
            disp = (prices[n] / ma_lines[k].iloc[-1]) - 1.0
            report.append(f"- {w}일: {'ON' if today_ma_states_dict[k] else 'OFF'} ({disp:+.1%})")
            
    return "\n".join(report)

if __name__ == "__main__":
    try:
        report_text = get_daily_signals_and_report()
        if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_TO, report_text):
            print("성공적으로 전송되었습니다.")
    except Exception as e:
        print(f"오류 발생: {e}")
        sys.exit(1)
