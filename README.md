Tactical Asset Allocation (TAA) Bot - 5-Asset MA Strategy

본 프로젝트는 5개 금융 투자 자산(한국/중국/인도 주식 3종 + 한국 채권 2종)에 이동평균선(MA) 기반의 전술적 비중 조절 전략을 적용하고, 그 결과를 텔레그램으로 자동 보고하는 Python 패키지입니다.

⚙️ 전략 설정

자산: 102110.KS, 283580.KS, 453810.KS, 148070.KS, 385560.KS

기본 비중 (Base Weight): 모든 자산 20% (균등 배분)

MA 지표: 20일, 120일, 200일 이동평균선

비중 조절: 3-MA 신호 개수에 따라 100%, 75%, 50%, 0%로 투자 비중 변경.

🚀 환경 설정 및 실행 방법

1. 종속성 설치

프로젝트 실행에 필요한 라이브러리를 설치합니다.

pip install yfinance numpy pandas requests


2. 환경 변수 설정 (필수)

텔레그램 메시지 발송을 위해 다음 두 가지 환경 변수를 설정해야 합니다. (GitHub Actions 또는 서버 환경에서 설정)

변수명

설명

TELEGRAM_TOKEN

발송에 사용할 텔레그램 봇 토큰입니다.

TELEGRAM_TO

메시지를 수신할 채팅방 ID (개인 ID 또는 그룹 ID)입니다.

3. 스케줄링 및 실행 (daily_run.yml)

daily_signal_generator.py는 **외부 스케줄러(Cron, GitHub Actions 등)**에 의해 매일 오전 8시(KST)에 실행되도록 설정해야 합니다.

daily_signal_generator.py 내부 로직이 자동으로 주말 여부를 판단하여 다음과 같이 처리합니다:

실행 시점

실행일

데이터 기준일

월요일 오전 8시

월요일

지난주 금요일 종가 기준

화~금요일 오전 8시

화~금요일

전일 (어제) 종가 기준

토~일요일

(실행하지 않음)

-

실행 명령어:

python daily_signal_generator.py
