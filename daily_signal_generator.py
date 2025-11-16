import yfinance as yf
import numpy as np
import pandas as pd
import datetime
import os
import requests
import sys
# ... (전략 설정 및 함수 정의 부분 생략) ...

# --- Telegram Transmission and Scheduling Logic ---
# ... (get_target_date, format_report 함수 생략) ...

if __name__ == "__main__":
    
    try:
        # Record execution time
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Auto Report execution started.")
        
        target_date = get_target_date()
        
        if target_date is None:
            # 주말이므로 실행을 건너뜀 (텔레그램 전송도 안함)
            sys.exit(0)
        
        # 1. Execute MA Strategy and calculate final weights
        weights, daily_return_info = run_ma_strategy_for_date(target_date)
        
        if weights is None:
            # 2. 실패 보고서 포맷 (데이터 다운로드 실패나 데이터 부족 시)
            final_output = f"❌ **MA Individual Strategy Report - Failed**\nBase Date: {target_date.strftime('%Y-%m-%d')}\nReason: {daily_return_info}"
        else:
            # 3. 성공 보고서 포맷
            final_output = format_report(target_date, weights, daily_return_info)
        
        # 4. 최종 보고서 내용을 표준 출력 (STDOUT)으로 출력 (GitHub Actions 캡처)
        print(final_output)

    except Exception as e:
        # 🚨 치명적인 오류 발생 시: 최소 200자 이상의 확실한 에러 메시지를 출력
        error_message = (
            f"❌ FATAL PYTHON ERROR ❌\n\n"
            f"Deployment failed. The script terminated unexpectedly during execution. "
            f"Please check the GitHub Actions detailed logs for the step 'Run MA Strategy Script and Capture Output'.\n\n"
            f"Error details (Partial):\n{str(e)[:200]}..." # 오류 메시지를 200자까지 포함
        )
        print(error_message, file=sys.stderr)
        sys.exit(1) # 오류 발생 시 비정상 종료 코드를 반환하여 Actions 로그에 표시
