#!/usr/bin/env python3
"""
TAA Satellite 시그널 — 국내 상장 8자산 동적 배분

코어(QQQ / TLT / GLD)와 별도 계좌로 독립 운용하는 위성 포트폴리오.
상태 판정 규칙은 코어와 완전히 동일하고, 유니버스와 비중만 다르다.

[상태 판정] 자산별로 20/120/200일선 각각 ON/OFF 상태를 추적한다.
  OFF -> ON : 종가 > MA * 1.015
  ON -> OFF : 종가 < MA * 0.975
  그 외      : 전일 상태 유지

  진입 문턱(+1.5%)과 이탈 문턱(-2.5%)이 달라 그 사이 구간에서는
  상태가 바뀌지 않는다(히스테리시스). 횡보장 휩소를 억제한다.

[포지션 비중] ON 개수로 자산별 기본 비중을 스케일링
  3개 -> 100%   2개 -> 75%   1개 -> 50%   0개 -> 0%
  기본 비중은 전 자산 20% 동일 -> 20% / 15% / 10% / 0%

  자산별 상한 20%만 적용하고 총합 상한은 두지 않는다. 각 자산은 다른
  자산의 상태와 무관하게 자기 점수로만 비중이 정해지므로, 유니버스에
  종목을 추가해도 규칙이 그대로다. 다만 다수가 동시에 ON이면 합계가
  100%를 넘을 수 있어 리포트에 초과분과 환산 비중을 함께 표시한다.

[주의] 히스테리시스는 경로 의존적이다. 짧은 기간만 받으면 상태가 0에서
  출발해 실제와 다른 신호가 나오고, 매일 시작점이 밀려 어제와 오늘의
  결과가 뒤집힌다. LOOKBACK 은 반드시 충분히 길게 유지할 것.

[집행] 당일 종가로 산출하고 다음 거래일에 집행한다(lag = 1).

환경변수:
  TELEGRAM_BOT_TOKEN  (필수, 구 TELEGRAM_TOKEN 도 인식)
  TELEGRAM_CHAT_ID    (필수, 구 TELEGRAM_TO 도 인식)
  PORTFOLIO_VALUE     (선택) 평가액(KRW). 지정 시 목표 수량(주)까지 계산
  ALWAYS_SEND         (선택) "false"면 비중 변동이 있을 때만 전송. 기본 true
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import yfinance as yf

# ===== 설정 ============================================================
TICKERS = {
    "102110.KS": "한국 주식",
    "283580.KS": "중국 주식",
    "241180.KS": "일본 주식",
    "453810.KS": "인도 주식",
    "385560.KS": "채권 30년",
    "148070.KS": "채권 10년",
    "426030.KS": "나스닥 주식",
    "0019K0.KS": "나스닥 채권",
}

BASE_WEIGHT = 0.20                                   # 자산별 상한(=기본 비중)
BASE_WEIGHTS = {t: BASE_WEIGHT for t in TICKERS}

MA_PERIODS = [20, 120, 200]

BAND_UP = 1.015         # 매수(ON) 문턱  MA +1.5%
BAND_DN = 0.975         # 매도(OFF) 문턱 MA -2.5%

SCALAR_MAP = {3: 1.00, 2: 0.75, 1: 0.50, 0: 0.00}

LOOKBACK = "max"        # 히스테리시스 상태 수렴을 위해 전체 히스토리 사용
WARMUP_EXTRA = 250      # 최소 요구 길이 = max(MA) + 이 값. 신규 상장 종목은 제외됨
RETRIES = 4
STALE_DAYS = 5          # 최신 데이터가 이보다 오래되면 경고
TG_MAX_LEN = 3800
KST = timezone(timedelta(hours=9))
# =======================================================================


def esc(s) -> str:
    """텔레그램 HTML 파싱 오류 방지."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def pct(w: float) -> str:
    """20% / 15% / 10% 는 정수, 환산 비중처럼 소수가 나오면 소수 1자리."""
    return f"{w:.0%}" if abs(w * 100 - round(w * 100)) < 1e-9 else f"{w:.1%}"


# ---------- 데이터 수집 -------------------------------------------------
def fetch_prices():
    """{ticker: Series} 반환. 실패 시 재시도."""
    tickers = list(TICKERS)
    last_err = None

    for attempt in range(1, RETRIES + 1):
        try:
            df = yf.download(
                tickers, period=LOOKBACK, interval="1d",
                auto_adjust=True, progress=False, threads=False,
                group_by="column",
            )
            if df is not None and not df.empty:
                close = df["Close"]
                out = {}
                if isinstance(close, pd.Series):
                    out[tickers[0]] = close.dropna()
                else:
                    for t in tickers:
                        if t in close.columns:
                            out[t] = close[t].dropna()
                return out, None
            last_err = "빈 응답"
        except Exception as e:                          # noqa: BLE001
            last_err = str(e)

        if attempt < RETRIES:
            wait = 4 * attempt
            print(f"  수신 실패({last_err}) — {wait}초 후 재시도 "
                  f"{attempt}/{RETRIES - 1}", file=sys.stderr)
            time.sleep(wait)

    return {}, last_err


# ---------- 상태 머신 ---------------------------------------------------
def compute_states(close: pd.Series):
    """(정보 dict, 오류메시지) 반환.

    백테스트와 동일한 규칙:
        price > ma * BAND_UP  -> ON
        price < ma * BAND_DN  -> OFF
        그 외                 -> 유지
    """
    need = max(MA_PERIODS) + WARMUP_EXTRA     # 히스테리시스 워밍업 여유
    if len(close) < need:
        return None, f"데이터 부족 ({len(close)}일 / 최소 {need}일)"

    mas = {n: close.rolling(n).mean() for n in MA_PERIODS}
    state = {n: 0 for n in MA_PERIODS}
    prev_snapshot = None

    for i in range(max(MA_PERIODS) - 1, len(close)):
        price = float(close.iloc[i])

        nxt = {}
        for n in MA_PERIODS:
            ma = mas[n].iloc[i]
            if pd.isna(ma):
                nxt[n] = 0
                continue
            ma = float(ma)
            s = state[n]
            if price > ma * BAND_UP:
                s = 1
            elif price < ma * BAND_DN:
                s = 0
            nxt[n] = s

        if i == len(close) - 1:
            prev_snapshot = dict(state)     # 마지막 봉 직전 상태
        state = nxt

    if prev_snapshot is None:
        return None, "상태 계산 실패"

    last = float(close.iloc[-1])
    before = float(close.iloc[-2])
    return {
        "today": dict(state),
        "yesterday": prev_snapshot,
        "ma": {n: float(mas[n].iloc[-1]) for n in MA_PERIODS},
        "price": last,
        "pct": (last / before - 1) * 100 if before else 0.0,
        "date": close.index[-1],
    }, None


# ---------- 리포트 ------------------------------------------------------
def build_report():
    prices, fetch_err = fetch_prices()

    now = datetime.now(KST).strftime("%Y-%m-%d")
    capital = os.environ.get("PORTFOLIO_VALUE", "").strip()
    capital = float(capital) if capital else None

    rows, changes, failed, warns = [], [], [], []
    base_date = None
    t_total = y_total = 0.0
    live = {}                       # 환산 비중 표시용 {name: weight}

    if fetch_err:
        warns.append(f"수신 경고: {fetch_err}")

    for ticker, name in TICKERS.items():
        close = prices.get(ticker)
        if close is None or close.empty:
            failed.append(f"{name} ({ticker}) — 데이터 없음")
            continue

        info, err = compute_states(close)
        if info is None:
            failed.append(f"{name} ({ticker}) — {err}")
            continue

        if base_date is None:
            base_date = info["date"]
            stale = (pd.Timestamp.now(tz="UTC").tz_localize(None)
                     - pd.Timestamp(base_date).tz_localize(None)).days
            if stale > STALE_DAYS:
                warns.append(f"데이터가 낡음 — 최종 {base_date.date()} ({stale}일 전)")

        t_on = sum(info["today"].values())
        y_on = sum(info["yesterday"].values())
        t_w = BASE_WEIGHTS[ticker] * SCALAR_MAP[t_on]
        y_w = BASE_WEIGHTS[ticker] * SCALAR_MAP[y_on]
        t_total += t_w
        y_total += y_w
        if t_w > 0:
            live[name] = t_w

        if abs(t_w - y_w) > 1e-9:
            flips = []
            for n in MA_PERIODS:
                if info["today"][n] > info["yesterday"][n]:
                    flips.append(f"{n}일↑")
                elif info["today"][n] < info["yesterday"][n]:
                    flips.append(f"{n}일↓")
            mark = "🔴" if t_w > y_w else "🔵"
            changes.append(
                f"{mark} <b>{esc(name)}</b>  {pct(y_w)} → <b>{pct(t_w)}</b>"
                f"  ({', '.join(flips)})"
            )

        dots = "".join("●" if info["today"][n] else "○" for n in MA_PERIODS)
        arrow = "▲" if info["pct"] > 0 else ("▼" if info["pct"] < 0 else "―")

        line = (f"{dots} <b>{pct(t_w)}</b>  {esc(name)}"
                f"  <code>{esc(ticker)}</code>\n"
                f"     ₩{fmt(info['price'])} {arrow}{abs(info['pct']):.1f}%")
        if capital:
            qty = capital * t_w / info["price"]
            line += f"  ·  {qty:,.0f}주"
        line += "\n     " + "  ".join(
            f"{n}일 {info['price'] / info['ma'][n] - 1:+.1%}" for n in MA_PERIODS
        )
        rows.append(line)

    t_cash, y_cash = 1.0 - t_total, 1.0 - y_total
    if rows and abs(t_cash - y_cash) > 1e-9:
        mark = "🔵" if t_cash > y_cash else "🔴"
        changes.append(
            f"{mark} <b>현금</b>  {pct(max(y_cash, 0))} → <b>{pct(max(t_cash, 0))}</b>"
        )

    lines = ["<b>🛰 TAA Satellite — 국내 8자산</b>", f"<i>{now} KST</i>"]
    if base_date is not None:
        lines.append(f"<i>기준: {base_date.strftime('%Y-%m-%d')} 마감 · 익일 집행</i>")
    lines.append("")

    if changes:
        lines.append(f"<b>■ 리밸런싱 필요 — {len(changes)}건</b>")
        lines += changes
    else:
        lines.append("<b>■ 리밸런싱 불필요</b>")
    lines.append("")

    lines.append("<b>■ 목표 비중</b>")
    lines.append("<i>● = 20/120/200일선 ON · 자산별 상한 20%</i>")
    lines += rows

    if rows:
        cash_line = f"　　 <b>{pct(max(t_cash, 0))}</b>  현금"
        if capital and t_cash > 0:
            cash_line += f"  ·  ₩{fmt(capital * t_cash)}"
        lines.append(cash_line)
        lines.append(f"　　 <i>위험자산 합계 {pct(t_total)}</i>")

    if t_total > 1.0 + 1e-9:
        lines += ["", f"<b>⚠️ 합계가 100%를 {pct(t_total - 1.0)}p 초과</b>",
                  "<i>100% 기준 환산 비중</i>"]
        for name, w in live.items():
            scaled = w / t_total
            entry = f"· {esc(name)}  <b>{scaled:.1%}</b>"
            if capital:
                entry += f"  ·  ₩{fmt(capital * scaled)}"
            lines.append(entry)

    if failed:
        lines += ["", "<b>⚠️ 처리 실패</b>"] + [f"· {esc(f)}" for f in failed]
    if warns:
        lines += ["", "<b>⚠️ 경고</b>"] + [f"· {esc(w)}" for w in warns]

    return "\n".join(lines), len(changes), len(rows)


# ---------- 텔레그램 ----------------------------------------------------
def send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or os.environ.get("TELEGRAM_TOKEN", "")).strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID")
               or os.environ.get("TELEGRAM_TO", "")).strip()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 전송 생략.",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > TG_MAX_LEN:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)

    ok = True
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패 {r.status_code}: {r.text}",
                      file=sys.stderr)
                ok = False
        except Exception as e:                          # noqa: BLE001
            print(f"텔레그램 전송 오류: {e}", file=sys.stderr)
            ok = False
        time.sleep(0.5)
    return ok


# ---------- 진입점 ------------------------------------------------------
def main():
    report, n_changes, n_ok = build_report()

    plain = report
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        plain = plain.replace(tag, "")
    print(plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))

    if n_ok == 0:
        print("\n전 종목 처리 실패.", file=sys.stderr)
        send_telegram(report)
        sys.exit(1)

    always = os.environ.get("ALWAYS_SEND", "true").lower() != "false"
    if n_changes == 0 and not always:
        print("\n변동이 없어 전송하지 않았습니다 (ALWAYS_SEND=false).")
        return

    if not send_telegram(report):
        sys.exit(1)
    print("\n텔레그램 전송 완료.")


if __name__ == "__main__":
    main()
