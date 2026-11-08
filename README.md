# CoinGecko 신규 코인 로컬 아카이버

CoinGecko 신규 코인 목록을 읽고, 내가 선택한 코인의 공식 사이트·SNS·백서를 로컬에 보관하는 개인용 도구입니다. 이제 **자동 예약 실행 대신 로컬 웹 페이지의 버튼으로만** 실행하며, **실사용 Chrome에 로그인된 세션과 즐겨찾기를 그대로** 사용할 수 있습니다.

- `목록 새로고침`: 최신 코인 목록만 읽어 CSV로 저장 (다운로드/캡처 없음)
- `원클릭 수집`: 선택한 코인(선택이 없으면 전체)의 스크린샷/PDF 저장
- `선택 즐겨찾기 추가`: 선택한 코인을 CoinGecko 즐겨찾기에 추가하고 결과 확인
- `ZIP 다운로드`: 최신 목록에 해당하는 파일만 ZIP으로 다운로드
- `전체 아카이브 ZIP`: 지금까지 저장한 모든 파일을 ZIP으로 다운로드

이 도구는 로그인 암호를 입력받거나 저장하지 않고, CAPTCHA·접근 제한을 우회하지 않습니다. 수집물에는 제3자 사이트의 저작권이 적용될 수 있으므로 공개 저장소에 올리지 말고 개인 용도로만 사용하세요. CoinGecko의 이용약관과 robots 정책을 확인하고 요청 빈도 및 사이트 지침을 준수하세요.

## 설치

Windows에서 Google Chrome과 Python 3.11 이상을 설치한 다음, 이 `collector` 폴더에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## 빠른 시작

1. **Chrome을 평소처럼 열고 CoinGecko에 로그인**합니다.
2. 주소창에 `chrome://inspect/#remote-debugging`을 입력합니다.
3. **Allow remote debugging for this browser instance**를 켭니다.
4. `run_web.cmd`를 더블클릭합니다. 웹 페이지가 자동으로 열립니다.
5. **목록 새로고침**을 누릅니다. Chrome에 승인 창이 뜨면 **허용**을 누릅니다.
6. 목록에서 코인을 고르거나 전체 선택한 뒤 **원클릭 수집** / **선택 즐겨찾기 추가**를 누릅니다.
7. **CSV** 또는 **ZIP 다운로드**로 결과를 받습니다.

웹 서버는 `127.0.0.1:8765`에서만 열립니다. 창을 닫으려면 실행 중인 검은 콘솔 창에서 `Ctrl+C`를 누르세요.

> Chrome 원격 디버깅은 **현재 Chrome 인스턴스에만** 적용됩니다. Chrome을 완전히 종료했다가 다시 열면 2~3번을 다시 해야 합니다. 사용이 끝나면 같은 설정을 꺼두는 것을 권장합니다.

## 실사용 Chrome에 연결하는 이유

Chrome 136 이후에는 기본 프로필 폴더로 `--remote-debugging-port`를 실행하는 방식이 차단됩니다. 대신 Chrome이 제공하는 **명시적 허용** 기능을 사용합니다.

- 쿠키를 복사하지 않습니다.
- 별도 프로필을 만들지 않습니다.
- 이미 로그인된 즐겨찾기(포트폴리오 별표) 상태를 그대로 읽습니다.
- 즐겨찾기 추가도 같은 세션에서 수행합니다.

프로그램은 Chrome이 남기는 `DevToolsActivePort` 파일을 읽어 로컬 CDP 주소로 연결합니다. 연결 시 Chrome 승인 창이 한 번 뜰 수 있습니다.

### 연결 확인

웹 페이지 상단에 **Chrome 연결 준비됨**이 표시되면 정상입니다. **Chrome 연결 필요**가 표시되면:

1. Chrome에서 `chrome://inspect/#remote-debugging`을 엽니다.
2. **Allow remote debugging for this browser instance**를 켭니다.
3. 웹 페이지에서 **Chrome 설정 열기**를 누르거나 직접 주소를 엽니다.
4. **목록 새로고침**을 다시 누릅니다.

그래도 안 되면 Chrome을 완전히 종료한 뒤 다시 실행하고 2번부터 반복하세요.

## 웹 UI 사용법

### 목록 새로고침

지정한 페이지 범위에서 등록 경과시간이 `최근 N일` 이내인 코인을 읽어 표시하고 `recent-coins-preview.csv`를 갱신합니다. 이 단계는 파일을 다운로드하거나 캡처하지 않습니다.

- **페이지**: 1~10
- **최근 N일**: 1~30
- **별표 상태와 무관하게 모두 수집**: 로그인/별표 상태를 확인할 수 없어도 모든 코인을 처리합니다. 기본값은 꺼짐(실패 폐쇄)입니다.
- **완료된 코인도 다시 수집**: 이미 완료된 항목도 강제로 다시 캡처합니다.

### 원클릭 수집

목록에서 선택한 코인만 처리합니다. 아무것도 선택하지 않으면 현재 목록 전체를 처리합니다.

기본 동작은 **실패 폐쇄**입니다. 별표가 명시적으로 활성인 코인만 캡처하고, 상태를 확신할 수 없으면 건너뛰고 `needs_review`로 기록합니다. `별표 상태와 무관하게 모두 수집`을 켜면 이 제한을 해제합니다.

코인마다 다음 순서로 저장합니다.

1. CoinGecko `Website` 링크 → 공식 사이트 전체 페이지 PNG
2. 웹사이트가 없으면 `Community`의 Twitter/X → 계정 화면 PNG
3. 둘 다 없으면 `Explorers` 링크 → 탐색기 화면 PNG
4. 백서가 실제 PDF면 원본 PDF 저장
5. GitBook 같은 웹 백서면 전체 PNG + 브라우저 인쇄 PDF 저장

완료된 코인은 다음 실행에서 건너뜁니다. `완료된 코인도 다시 수집`을 켜면 다시 처리합니다.

### 선택 즐겨찾기 추가

목록에서 코인을 선택한 뒤 버튼을 누릅니다. CoinGecko의 별표를 실제로 클릭하고, 별표가 활성 상태로 바뀌었는지 다시 확인합니다.

- 이미 활성: `already_active`
- 추가 성공: `added`
- 로그인 필요: `login_required`
- 행/별표를 찾지 못함: `row_not_found`, `star_not_found`
- 확인 실패: `verification_failed`

실패한 항목은 성공으로 추정하지 않고 매니페스트와 결과 JSON에 그대로 기록합니다.

### 다운로드

- **CSV**: 현재 목록 `recent-coins-preview.csv`
- **ZIP 다운로드**: 최신 목록에 해당하는 캡처 파일 + CSV + 리포트 + 매니페스트 (보통 수 MB)
- **전체 아카이브 ZIP**: 지금까지 저장한 모든 파일 (수백 MB가 될 수 있음)

ZIP은 이미 압축된 PNG/PDF를 다시 압축하지 않으며, 원본 파일이 바뀌지 않았으면 이전 ZIP을 재사용합니다.

### 예약 작업 끄기

상단/하단의 **예약 작업 끄기** 버튼은 Windows 작업 스케줄러의 `CoinGecko-Weekly-Listing` 작업을 비활성화합니다. 자동 실행을 원하지 않으면 이 버튼으로 꺼두세요. 다시 켜려면 같은 버튼을 누릅니다.

## 이름이 겹치는 코인 구분

CoinGecko에는 이름과 심볼이 같은 코인이 여러 개 있을 수 있습니다. 화면 표시, CSV, 파일 이름에 다음 구분자를 넣습니다.

```text
Auren Protocol (AU) [auren-protocol]
Example (EX) [example]
Example (EX) #2 [example-2]
```

- `(AU)`: 심볼
- `[auren-protocol]`: CoinGecko slug
- `#2`: 같은 이름+심볼이 한 결과 안에서 두 번째 이상일 때만 붙는 번호

파일 이름도 `코인명 (심볼)-slug-짧은해시-종류.png` 형식이라 동명 코인과 여러 링크가 섞이지 않습니다.

## 저장 위치

기본 출력 폴더는 `collector`의 상위 폴더, 즉 `CoinGecko Archive`입니다.

```text
CoinGecko Archive\
  recent-coins-preview.csv
  capture-report.txt
  .state\manifest.json
  .state\web-last-scan.json
  .state\web-last-result.json
  .state\web-last-favorites.json
  .state\web-logs\
  archive\screenshots\
  archive\whitepapers\
```

다른 폴더를 쓰려면:

```powershell
python web_app.py --output-dir "D:\내 아카이브"
```

## 고급: CLI로 실행

웹 UI 없이 명령줄로도 실행할 수 있습니다. 실사용 Chrome에 연결하려면 `--attach`를 사용합니다.

```powershell
$output = "C:\Users\onlya\OneDrive\Desktop\CoinGecko Archive"

# 목록만 확인
python .\coingecko_archive.py --mode dry-run --pages 10 --since-days 1 `
  --attach --output-dir $output --result-json "$output\.state\cli-scan.json"

# 전체 수집 (실패 폐쇄)
python .\coingecko_archive.py --mode capture --pages 10 --since-days 1 `
  --attach --output-dir $output --result-json "$output\.state\cli-capture.json"

# 선택한 코인만 수집
python .\coingecko_archive.py --mode capture --pages 10 --since-days 1 `
  --attach --selection-json "$output\.state\selection.json" `
  --output-dir $output --result-json "$output\.state\cli-capture.json"

# 선택한 코인을 즐겨찾기에 추가
python .\coingecko_archive.py --mode favorites --pages 10 --since-days 1 `
  --attach --selection-json "$output\.state\selection.json" `
  --output-dir $output --result-json "$output\.state\cli-favorites.json"
```

`selection.json`은 문자열 배열 또는 `{"coin_url": "..."}` 객체 배열을 받습니다.

```json
{"urls": ["https://www.coingecko.com/en/coins/auren-protocol"]}
```

### 전용 프로필 대체 방법

실사용 Chrome 연결이 불가능할 때만 사용하세요. 이 경우 기존 Chrome 로그인과 즐겨찾기는 공유되지 않습니다.

```powershell
python .\coingecko_archive.py --setup-profile
```

열린 전용 브라우저 창에서 직접 로그인한 뒤 **그 창만** 닫고, `--attach` 없이 실행합니다. 웹 UI는 항상 실사용 Chrome 연결을 사용합니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| `Chrome 연결 필요` | `chrome://inspect/#remote-debugging`에서 허용을 켜고 다시 시도 |
| 승인 창이 안 뜸 | Chrome을 완전히 종료 후 재실행, 원격 디버깅 다시 허용 |
| `login_required` | 연결된 Chrome에서 CoinGecko에 로그인되어 있는지 확인 |
| `ProcessSingleton` | 전용 프로필 브라우저 창이 열려 있음. 그 창만 닫기 |
| 이미 실행 중 | 웹 UI 작업이 끝날 때까지 기다리거나 **실행 중지** |
| ZIP이 큼 | **ZIP 다운로드**(최신 목록) 대신 **전체 아카이브 ZIP**을 사용하지 않기 |
| 심볼이 비어 있음 | 오래된 CSV일 수 있음. **목록 새로고침**으로 다시 생성 |

## 개발 및 테스트

```powershell
python -m pytest -q
```

현재 테스트는 다음을 포함합니다.

- 목록 파서의 이름/심볼 분리
- 로그인되지 않은 경우에도 심볼이 유지되는지
- 이름+심볼 중복 시 `#2` 구분자
- 파일 이름의 심볼/slug 구분자
- `DevToolsActivePort` 파싱
- 선택 JSON 로드
- PDF/스크린샷 저장 및 실패 폐쇄 동작

## 개인정보와 저장소 경계

이 저장소에는 프로그램 코드와 문서만 포함해야 합니다. 스크린샷, PDF, 매니페스트, 브라우저 프로필, 로그는 로컬 전용이며 커밋하지 마세요. `.gitignore`가 `.state\`, `archive\`, `recent-coins-preview.csv`, `capture-report.txt`, 로그를 제외합니다.

원격 디버깅은 강력한 기능입니다. 사용하는 동안에만 켜고, 끝나면 끄세요. 웹 서버는 `127.0.0.1`에만 바인딩되지만 다른 로컬 프로그램이 접근할 수 있으므로 신뢰할 수 없는 네트워크나 공용 PC에서는 실행하지 마세요.
