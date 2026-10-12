# CoinGecko 신규 코인 로컬 아카이버

CoinGecko 신규 코인 목록의 지정 페이지를 읽고, 포트폴리오 별표가 **명시적으로 활성인** 코인만 로컬에 보관합니다. 별표 상태를 확신할 수 없는 행은 실패 폐쇄(fail closed) 원칙으로 캡처하지 않고 검토 대상으로 표시합니다. 비활성 코인은 건너뜁니다.

이 도구는 CoinGecko 페이지와 공개 링크를 읽고 로컬 브라우저에서 스크린샷/PDF를 저장합니다. 로그인 암호를 입력받거나 저장하지 않고, CAPTCHA·접근 제한을 우회하지 않습니다. 실제 수집물에는 제3자 사이트의 저작권이 적용될 수 있으므로 공개 저장소에 올리지 말고 개인 용도로만 사용하세요. CoinGecko의 이용약관과 robots 정책을 확인하고, 요청 빈도 및 사이트 지침을 준수하세요.

## 설치

Windows에서 Google Chrome과 Python 3.11 이상을 설치한 다음 저장소 폴더에서 실행합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Playwright는 `--browser chrome`(기본값) 또는 `--browser edge`로 선택한 브라우저의 실행 파일 경로를 직접 지정합니다. 선택한 브라우저가 없으면 다른 브라우저로 대신 실행하지 않습니다. 자동화 전용 프로필은 기본적으로 `%LOCALAPPDATA%\CoinGeckoArchive\chrome-profile` 또는 `%LOCALAPPDATA%\CoinGeckoArchive\edge-profile`에 만들어지며, 일반 프로필과 분리됩니다. 기존 브라우저에 로그인되어 있어도 전용 프로필에서 별도로 로그인해야 합니다.

## 전용 프로필 로그인

먼저 프로필 준비 명령을 실행하세요.

```powershell
# Chrome 전용 프로필
python .\coingecko_archive.py --setup-profile

# Edge 전용 프로필
python .\coingecko_archive.py --browser edge --setup-profile
```

열린 전용 브라우저 창에서 CoinGecko에 직접 로그인하고 포트폴리오 별표 상태가 표시되는지 확인하세요. 이 명령은 로그인 입력을 요구하지 않고 창을 열고 종료합니다. 로그인 후 미리보기를 시작하기 전에 전용 브라우저 창만 닫으세요. 로그인 정보는 이 프로그램이나 저장소에 입력하지 않습니다. 프로필 폴더에는 브라우저 로그인 세션 정보가 포함될 수 있으므로 다른 사람과 공유하거나 백업 저장소에 올리지 마세요.

미리보기/수집 프로그램을 실행하기 전에 `--setup-profile` 명령이 끝나 전용 브라우저 창이 닫혔는지 확인하세요. 해당 프로필이 이미 다른 창에서 사용 중이면 새 실행은 충돌 안내와 함께 중단됩니다. 다른 일반 Chrome/Edge 창은 닫지 않아도 됩니다.

## 실행

미리보기는 처음 10페이지에서 CoinGecko가 표시한 등록 경과시간이 7일 이하이며 날짜를 판독할 수 있는 코인 목록을 최근 등록순으로 출력하고 `recent-coins-preview.csv`로 저장합니다. 상대 시간 표시를 기준으로 한 목록이므로 `7 days` 항목도 포함됩니다. 캡처와 다운로드는 하지 않습니다. 기본 실행은 코드 폴더를 출력 위치로 사용하지만, `--output-dir`로 개인 데이터 경로를 지정할 수 있습니다.

```powershell
$output = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CoinGecko Archive'
python .\coingecko_archive.py --browser edge --mode dry-run --pages 10 --since-days 7 --output-dir $output
```

실제 수집은 다음 명령으로 실행합니다.

```powershell
$output = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CoinGecko Archive'
python .\coingecko_archive.py --browser edge --mode capture --pages 10 --since-days 7 --output-dir $output
```

`--pages`는 1부터 10까지만 허용됩니다. 실행 중 접근 확인/CAPTCHA 화면을 만나면 자동으로 우회하지 않고 중단합니다. 별표가 비활성인 코인은 건너뛰고, 별표를 판정할 수 없는 코인은 검토용 기록만 남깁니다. 첫 실행 전에 미리보기를 확인하고, 개인 계정에 로그인한 전용 프로필에서 수동으로 실제 수집을 시작하세요.

일반 Chrome/Edge 창에 이미 로그인되어 있어도 프로그램은 그 창에 연결하거나 기존 프로필 쿠키를 읽지 않습니다. 전용 프로필을 사용하는 것은 브라우저를 강제 종료하거나 로그인 상태를 복사하지 않기 위한 안전 조치입니다. 따라서 일반 프로필에서만 로그인한 경우 미리보기 별표 상태는 `unknown`으로 나올 수 있으며, 이 경우 수집하지 않습니다. PowerShell의 코드 페이지에서 표시할 수 없는 코인 문자는 실행이 중단되지 않도록 이스케이프해 출력합니다.

공식 웹사이트의 전체 페이지 스크린샷은 출력 폴더의 `archive\screenshots`에, 실제 PDF임이 `%PDF-` 파일 시그니처로 확인된 백서는 `archive\whitepapers`에 저장됩니다. PDF가 아닌 백서는 수집 후 선택한 전용 브라우저 프로필의 새 창/탭으로 열어 둡니다. 결과 상태와 검토 사유는 `.state\manifest.json`에 기록됩니다. 기본 프로필/출력 경로는 `.gitignore`에 포함되어 있습니다. 완료된 코인은 다음 실행에서 건너뛰고, 실패/검토 상태는 다음 실행에서 재시도합니다. 각 코인 처리가 끝날 때마다 매니페스트를 저장합니다.

파일 이름은 Windows에서 사용할 수 없는 문자를 치환하고 코인 URL 해시를 덧붙여 동명 코인도 구별합니다. 다운로드는 순차적으로 수행하고 재시도 횟수와 PDF 크기에 제한을 둡니다.

## 매일 오후 7시(KST) 예약

매일 최근 7일 목록 CSV만 저장하려면 Windows **작업 스케줄러**에서 다음처럼 작업을 설정합니다. 이 작업은 페이지 목록만 갱신하며 웹사이트 스크린샷이나 PDF를 수집하지 않습니다.

1. 작업 만들기에서 트리거를 매일 오후 7시로 지정합니다. Windows 시간대가 한국 표준시인지 확인하세요.
2. 보안 옵션에서 **사용자가 로그온할 때만 실행**을 선택합니다. 이 작업은 대화형 Edge를 여는 로컬 프로필을 사용합니다.
3. 동작은 `프로그램 시작`으로 설정합니다. 프로그램은 Python의 전체 경로, 인수는 `coingecko_archive.py --browser edge --mode dry-run --pages 10 --since-days 7 --output-dir "C:\path\to\CoinGecko Archive"`, 시작 위치는 수집기 코드 폴더로 지정합니다.
4. 이미 실행 중인 작업은 **새 인스턴스를 시작하지 않음**으로 설정합니다.

예약 작업은 현재 Windows 사용자가 로그인한 세션에서 실행됩니다. `--mode capture`는 별도 전용 프로필의 수동 로그인과 포트폴리오 별표 상태 확인 후에만 사용하세요. 일반 Edge 로그인은 전용 프로필에 공유되지 않으며, CAPTCHA가 나오면 자동으로 우회하지 말고 중단합니다.

## 개발 및 테스트

```powershell
pytest
```

## 개인정보와 저장소 경계

이 저장소에는 프로그램 코드와 문서만 포함해야 합니다. 스크린샷, PDF, 매니페스트, Chrome 프로필, 로그는 로컬 전용이며 커밋하지 마세요. 별표/공식 링크가 명확하지 않은 항목은 매니페스트의 검토 사유를 확인한 후 수동으로 처리하세요.
