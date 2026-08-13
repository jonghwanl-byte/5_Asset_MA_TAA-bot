#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TAA Satellite Signal Bot
========================

QQQ / TLT / GLD 코어 전략과 **별개로** 운용하는 위성(satellite) 8자산 TAA 봇.

시그널 규칙은 코어 TAA와 완전히 동일하다.

    1) 3개 이동평균(20 / 120 / 200일)에 대해 히스테리시스 밴드로 ON/OFF 판정
         - OFF -> ON : 종가 >  MA * (1 + 0.015)      (매수 진입: +1.5%)
         - ON  -> OFF: 종가 <  MA * (1 - 0.025)      (매도 이탈: -2.5%)
         - 밴드 사이(중립 구간)에서는 직전 상태를 그대로 유지
    2) 자산별 점수 = ON 개수 (0 ~ 3)
    3) 자산별 비중 = 기준비중(20%) * {3: 1.00, 2: 0.75, 1: 0.50, 0: 0.00}
                   = 20% / 15% / 10% / 0%
    4) 자산별 상한 20%만 적용하고 총합 상한은 두지 않는다 (MAX_GROSS = None).
       각 자산은 다른 자산의 상태와 무관하게 자기 점수로만 비중이 정해지므로,
       유니버스에 종목을 추가해도 규칙은 동일하다. 잔여분은 현금.
    5) 시그널은 당일 종가로 산출하고 **다음 거래일에 집행** (lag = 1)

상태(state)는 파일에 저장하지 않는다. 매 실행마다 HISTORY_PERIOD 만큼의
과거 데이터로 히스테리시스를 처음부터 재현하므로 실행 환경이 초기화되어도
동일한 결과가 재현된다(stateless / reproducible).

환경변수
    TELEGRAM_TOKEN : 텔레그램 봇 토큰
    TELEGRAM_TO    : 수신 chat_id
    (미설정 시 콘솔로만 출력하고 정상 종료 -> 로컬 테스트용)
"""

from __future__ import annotations

import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf


# ──────────────────────────────────────────────────────────────────────────────
# 1. 전략 파라미터
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Asset:
    name: str
    ticker: str


ASSETS: List[Asset] = [
    Asset("한국 주식",        "102110.KS"),
    Asset("중국 주식",        "283580.KS"),
    Asset("일본 주식",        "241180.KS"),
    Asset("인도 주식",        "453810.KS"),
    Asset("채권 30년",        "385560.KS"),
    Asset("채권 10년",        "148070.KS"),
    Asset("나스닥 주식",      "426030.KS"),
    Asset("나스닥 채권",      "0019K0.KS"),
]

MA_WINDOWS: Sequence[int] = (20, 120, 200)

# 히스테리시스 밴드 (코어 TAA와 동일: +1.5% / -2.5%)
UPPER_BAND_MULT = 1.015
LOWER_BAND_MULT = 0.975

# 배분 규칙: 기준비중 20% * 스칼라 -> 20 / 15 / 10 / 0 %
BASE_WEIGHT = 0.20
SCALAR_MAP: Dict[int, float] = {3: 1.00, 2: 0.75, 1: 0.50, 0: 0.00}

# 위험자산 총합 상한.
#   None = 상한 없음. 각 자산은 오직 자신의 점수로만 비중이 결정되며(자산별 최대 20%),
#          다른 자산의 상태에 영향을 받지 않는다. 종목 수가 늘어도 규칙은 동일하다.
#   숫자를 넣으면(예: 1.00) 합계 초과분을 전 자산 비례 축소한다.
MAX_GROSS: Optional[float] = None

# 히스테리시스 워밍업을 위한 데이터 기간 (200일 MA + 충분한 상태 수렴 구간)
HISTORY_PERIOD = "3y"

# 리밸런싱 판정 임계치 (비중 차이가 이보다 작으면 '유지'로 간주)
REBAL_EPS = 0.001

KST = pytz.timezone("Asia/Seoul")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_TO = os.environ.get("TELEGRAM_TO")

MIN_OBS = max(MA_WINDOWS) + 20  # 이보다 관측치가 적은 자산은 '데이터 부족'으로 제외


# ──────────────────────────────────────────────────────────────────────────────
# 2. 데이터 수집
# ──────────────────────────────────────────────────────────────────────────────

def download_prices(
    tickers: Sequence[str],
    period: str = HISTORY_PERIOD,
    retries: int = 3,
    pause: float = 5.0,
) -> pd.DataFrame:
    """수정종가 시계열을 내려받는다. 실패 시 지수 백오프로 재시도."""
    last_err: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            raw = yf.download(
                list(tickers),
                period=period,
                interval="1d",
                auto_adjust=True,     # 'Close' 가 곧 수정종가
                progress=False,
                threads=False,
            )
            if raw is None or raw.empty:
                raise ValueError("yfinance 가 빈 데이터를 반환했습니다.")

            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Close"].copy()
            else:  # 단일 티커
                close = raw[["Close"]].copy()
                close.columns = [tickers[0]]

            close = close.reindex(columns=list(tickers))
            close.index = pd.to_datetime(close.index)
            close = close.sort_index().ffill()
            return close

        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"[WARN] 다운로드 실패 ({attempt}/{retries}): {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(pause * attempt)

    raise RuntimeError(f"가격 데이터 다운로드에 최종 실패했습니다: {last_err}")


# ──────────────────────────────────────────────────────────────────────────────
# 3. 시그널 계산
# ──────────────────────────────────────────────────────────────────────────────

def hysteresis_state(
    price: pd.Series,
    ma: pd.Series,
    upper_mult: float = UPPER_BAND_MULT,
    lower_mult: float = LOWER_BAND_MULT,
) -> pd.Series:
    """단일 (자산 x 이동평균) 조합의 히스테리시스 ON/OFF 상태 시계열.

    OFF 상태에서는 상단 밴드를 '초과'해야 ON,
    ON  상태에서는 하단 밴드 '이상'이면 ON 유지.
    데이터가 없는 구간은 NaN 이며 상태는 OFF 로 리셋된다.
    """
    p = price.to_numpy(dtype=float)
    m = ma.to_numpy(dtype=float)
    upper = m * upper_mult
    lower = m * lower_mult

    out = np.full(len(p), np.nan)
    prev = 0.0
    for i in range(len(p)):
        if np.isnan(p[i]) or np.isnan(m[i]):
            prev = 0.0
            continue
        if prev == 1.0:
            prev = 1.0 if p[i] >= lower[i] else 0.0
        else:
            prev = 1.0 if p[i] > upper[i] else 0.0
        out[i] = prev

    return pd.Series(out, index=price.index)


def compute_states(close: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """자산별 {날짜 x 이동평균} 상태 테이블을 만든다."""
    states: Dict[str, pd.DataFrame] = {}
    for asset in ASSETS:
        px = close[asset.ticker]
        cols = {}
        for w in MA_WINDOWS:
            ma = px.rolling(window=w, min_periods=w).mean()
            cols[w] = hysteresis_state(px, ma)
        states[asset.name] = pd.DataFrame(cols, index=close.index)
    return states


def scores_at(states: Dict[str, pd.DataFrame], pos: int) -> Dict[str, Optional[int]]:
    """pos 번째 행(음수 인덱스 허용)의 자산별 점수. 데이터 부족이면 None."""
    out: Dict[str, Optional[int]] = {}
    for name, df in states.items():
        row = df.iloc[pos]
        out[name] = None if row.isna().any() else int(row.sum())
    return out


def scores_to_weights(scores: Dict[str, Optional[int]]) -> Dict[str, float]:
    """점수 -> 목표 비중 (자산별 최대 BASE_WEIGHT).

    MAX_GROSS 가 None 이면 자산 간 상호작용 없이 각자의 점수만으로 비중이 정해진다.
    값이 주어진 경우에만 합계 초과분을 비례 축소한다.
    """
    raw = {
        name: (0.0 if s is None else BASE_WEIGHT * SCALAR_MAP[s])
        for name, s in scores.items()
    }
    if MAX_GROSS is None:
        return raw

    gross = sum(raw.values())
    if gross > MAX_GROSS + 1e-9:
        factor = MAX_GROSS / gross
        raw = {k: v * factor for k, v in raw.items()}
    return raw


# ──────────────────────────────────────────────────────────────────────────────
# 4. 리포트 생성
# ──────────────────────────────────────────────────────────────────────────────

def _pad(text: str, width: int) -> str:
    """한글(전각)을 2칸으로 계산해 폭을 맞춘다."""
    w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - w)


def _fmt_date(ts: pd.Timestamp) -> str:
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize(KST) if ts.tzinfo is None else ts.tz_convert(KST)
    return ts.strftime("%Y-%m-%d (%a)")


def build_report(
    close: pd.DataFrame,
    states: Dict[str, pd.DataFrame],
) -> str:
    today_scores = scores_at(states, -1)
    prev_scores = scores_at(states, -2)
    today_w = scores_to_weights(today_scores)
    prev_w = scores_to_weights(prev_scores)

    t_gross = sum(today_w.values())
    y_gross = sum(prev_w.values())
    t_cash, y_cash = 1.0 - t_gross, 1.0 - y_gross

    changed = [
        a.name for a in ASSETS
        if abs(today_w[a.name] - prev_w[a.name]) >= REBAL_EPS
    ]

    prices = close.iloc[-1]
    chg = close.pct_change().iloc[-1]
    stale = [a.name for a in ASSETS if today_scores[a.name] is None]

    L: List[str] = []
    L.append("[ TAA Satellite - 8 Asset ]")
    L.append(f"기준일: {_fmt_date(close.index[-1])} 종가")
    L.append("집행: 다음 거래일 (lag=1)")
    L.append(f"밴드: +{(UPPER_BAND_MULT-1)*100:.1f}% / -{(1-LOWER_BAND_MULT)*100:.1f}%"
             f"  |  배분: 20/15/10/0%")

    L.append("")
    if changed:
        L.append(f"[!] 리밸런싱 필요 - 변경 자산 {len(changed)}개: {', '.join(changed)}")
    else:
        L.append("[-] 리밸런싱 불필요 (전일과 동일)")
    if stale:
        L.append(f"[!] 데이터 부족으로 제외된 자산: {', '.join(stale)}")

    # [1] 목표 비중
    L.append("")
    L.append("-" * 28)
    L.append("[1] 오늘 목표 비중")
    for a in ASSETS:
        mark = "> " if a.name in changed else "  "
        L.append(f"{mark}{_pad(a.name, 12)}{today_w[a.name]:>6.1%}")
    cash_mark = "> " if abs(t_cash - y_cash) >= REBAL_EPS else "  "
    L.append(f"{cash_mark}{_pad('현금', 12)}{max(t_cash, 0.0):>6.1%}")

    if MAX_GROSS is None:
        L.append(f"  (위험자산 합계 {t_gross:.1%} / 자산별 상한 {BASE_WEIGHT:.0%})")
        if t_gross > 1.0 + 1e-9:
            L.append(f"  [!] 합계가 100%를 {t_gross - 1.0:.1%}p 초과합니다.")
            L.append("      100% 기준 환산 비중:")
            for a in ASSETS:
                if today_w[a.name] > 0:
                    L.append(f"        {_pad(a.name, 12)}{today_w[a.name] / t_gross:>6.1%}")
    else:
        L.append(f"  (위험자산 합계 {t_gross:.1%} / 상한 {MAX_GROSS:.0%})")
        if t_gross >= MAX_GROSS - 1e-9:
            L.append("  * 상한 도달: 개별 비중이 비례 축소되었습니다.")

    # [2] 변경 상세
    L.append("")
    L.append("-" * 28)
    L.append("[2] 비중 변경 상세")
    rows = [(a.name, prev_w[a.name], today_w[a.name]) for a in ASSETS]
    rows.append(("현금", max(y_cash, 0.0), max(t_cash, 0.0)))
    for name, yw, tw in rows:
        diff = tw - yw
        tag = "(유지)" if abs(diff) < REBAL_EPS else f"({diff:+.1%}p)"
        L.append(f"- {_pad(name, 12)}: {yw:>6.1%} -> {tw:>6.1%} {tag}")

    # [3] 시장 현황
    L.append("")
    L.append("-" * 28)
    L.append("[3] 종가 / 전일 대비")
    for a in ASSETS:
        px, c = prices[a.ticker], chg[a.ticker]
        if pd.isna(px):
            L.append(f"- {a.name}: 데이터 없음")
        else:
            L.append(f"- {a.name}: {px:,.0f} ({c:+.1%})" if pd.notna(c)
                     else f"- {a.name}: {px:,.0f}")

    # [4] MA 신호 상세
    L.append("")
    L.append("-" * 28)
    L.append("[4] MA 신호 상세 (이격도 = 종가/MA - 1)")
    for a in ASSETS:
        s = today_scores[a.name]
        if s is None:
            L.append(f"\n* {a.name} (데이터 부족)")
            continue
        L.append(f"\n* {a.name} ({s}/3) -> {today_w[a.name]:.1%}")
        px = close[a.ticker]
        for w in MA_WINDOWS:
            ma = px.rolling(window=w, min_periods=w).mean().iloc[-1]
            state = states[a.name][w].iloc[-1]
            disp = px.iloc[-1] / ma - 1.0
            L.append(f"  - {str(w).rjust(3)}일: {'ON ' if state == 1.0 else 'OFF'} ({disp:+.1%})")

    return "\n".join(L)


# ──────────────────────────────────────────────────────────────────────────────
# 5. 텔레그램 전송
# ──────────────────────────────────────────────────────────────────────────────

def chunk_text(text: str, limit: int = 3800) -> List[str]:
    """텔레그램 4096자 제한을 피해 줄 단위로 분할."""
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def send_telegram(token: Optional[str], chat_id: Optional[str], text: str,
                  retries: int = 3) -> bool:
    """parse_mode 없이 순수 텍스트로 전송 (마크다운 파싱 400 에러 방지)."""
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_TO 미설정 - 전송을 건너뜁니다.",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in chunk_text(text):
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, data={"chat_id": chat_id, "text": chunk},
                                  timeout=20)
                r.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 텔레그램 전송 실패 ({attempt}/{retries}): {exc}",
                      file=sys.stderr)
                if attempt == retries:
                    ok = False
                else:
                    time.sleep(3 * attempt)
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# 6. 엔트리 포인트
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tickers = [a.ticker for a in ASSETS]
    print("... 시장 데이터 다운로드 중 ...")
    close = download_prices(tickers)

    usable = [a.name for a in ASSETS if close[a.ticker].notna().sum() >= MIN_OBS]
    if not usable:
        raise RuntimeError("사용 가능한 자산이 없습니다. 티커/데이터를 확인하세요.")

    states = compute_states(close)
    report = build_report(close, states)

    print(report)
    send_telegram(TELEGRAM_TOKEN, TELEGRAM_TO, report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {exc}", file=sys.stderr)
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_TO,
                      f"[ TAA Satellite ] 실행 실패\n{exc}")
        sys.exit(1)
