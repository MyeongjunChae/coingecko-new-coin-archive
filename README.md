# CoinGecko 신규 코인 로컬 아카이버

CoinGecko 신규 코인 목록을 읽고, 내가 선택한 코인의 공식 사이트·SNS·백서를 로컬에 보관하는 개인용 도구입니다. 이제 **자동 예약 실행 대신 로컬 웹 페이지의 버튼으로만** 실행하며, **실사용 Chrome에 로그인된 세션과 즐겨찾기를 그대로** 사용할 수 있습니다.

- `목록 새로고침`: 최신 코인 목록만 읽어 CSV로 저장 (다운로드/캡처 없음)
- `원클릭 수집`: 선택한 코인(선택이 없으면 전체)의 스크린샷/PDF 저장
- `선택 즐겨찾기 추가`: 선택한 코인을 CoinGecko 즐겨찾기에 추가하고 결과 확인
- `임시코인 문서 캡처 (임시)`: Chrome 북마크 `임시코인` 폴더의 문서를 캡처하고 연결된 PDF를 저장
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
5. 아래의 **Chrome 북마크 연동** 절차대로 로컬 확장 프로그램을 한 번 로드하고, 웹 페이지를 새로고침합니다.
6. **목록 새로고침**을 누릅니다. Chrome이 허용 창을 띄우면 **딱 한 번만** 눌러 주세요.
7. 목록에서 코인을 고르거나 전체 선택한 뒤 **원클릭 수집** / **선택 즐겨찾기 추가**를 누릅니다.
8. **CSV** 또는 **ZIP 다운로드**로 결과를 받습니다.

웹 서버는 `127.0.0.1:8765`에서만 열립니다. 창을 닫으려면 실행 중인 검은 콘솔 창에서 `Ctrl+C`를 누르세요.

> Chrome 원격 디버깅은 **현재 Chrome 인스턴스에만** 적용됩니다. Chrome을 완전히 종료했다가 다시 열면 2~3번을 다시 해야 합니다. 사용이 끝나면 같은 설정을 꺼두는 것을 권장합니다.

> **허용 창은 Chrome 세션마다 한 번만 뜹니다.** 도구는 `127.0.0.1:9223`에 작은 로컬 릴레이(`collector\cdp_relay.py`)를 띄워 Chrome과의 연결을 하나만 유지합니다. 예전에는 수집할 때마다 허용 창이 떠서 계속 눌러야 했지만, 이제는 첫 실행에서 한 번만 누르면 이후 모든 수집이 같은 연결을 재사용합니다.

## 실사용 Chrome에 연결하는 이유

Chrome 136 이후에는 기본 프로필 폴더로 `--remote-debugging-port`를 실행하는 방식이 차단됩니다. 대신 Chrome이 제공하는 **명시적 허용** 기능을 사용합니다.

- 쿠키를 복사하지 않습니다.
- 별도 프로필을 만들지 않습니다.
- 이미 로그인된 즐겨찾기(포트폴리오 별표) 상태를 그대로 읽습니다.
- 즐겨찾기 추가도 같은 세션에서 수행합니다.

프로그램은 Chrome이 남기는 `DevToolsActivePort` 파일을 읽어 로컬 CDP 주소를 찾고, 릴레이를 통해 한 번만 허용을 받은 연결로 붙습니다.

릴레이 상태는 아래 명령으로 확인/중지할 수 있습니다.

```
python collector\cdp_relay.py --status
python collector\cdp_relay.py --stop
```

### 연결 확인

웹 페이지 상단에 **Chrome 디버깅 포트 감지됨**이 표시되면 포트가 열려 있다는 뜻입니다. 실제 CDP 연결은 목록/수집 실행 시 확인됩니다. **Chrome 연결 필요**가 표시되면:

1. Chrome에서 `chrome://inspect/#remote-debugging`을 엽니다.
2. **Allow remote debugging for this browser instance**를 켭니다.
3. 웹 페이지에서 **Chrome 설정 열기**를 누르거나 직접 주소를 엽니다.
4. **목록 새로고침**을 다시 누릅니다.

그래도 안 되면 Chrome을 완전히 종료한 뒤 다시 실행하고 2번부터 반복하세요.

### Chrome 북마크 연동

수집과 CoinGecko 포트폴리오 추가가 끝난 코인 페이지는 Chrome 북마크의 `My space > SITES > Cryptocurrency Forums > 완료 똥코인` 폴더에 추가됩니다. 이미 같은 URL이 이 폴더에 있으면 중복으로 만들지 않습니다. 임시 문서 캡처 버튼은 북마크의 `임시코인` 폴더를 읽습니다.

1. Chrome 주소창에서 `chrome://extensions`를 엽니다.
2. **개발자 모드**를 켜고 **압축해제된 확장 프로그램을 로드합니다**를 선택합니다.
3. 이 프로젝트의 `collector\bookmark_extension` 폴더를 선택합니다.
4. CoinGecko Archive 웹 페이지로 돌아와 새로고침합니다.

확장 프로그램은 Chrome의 북마크 읽기/추가 권한을 요청합니다. 콘텐츠 스크립트는 로컬 `127.0.0.1:8765` 앱 페이지에서만 실행되며, 북마크 URL은 로컬 앱에서 캡처하거나 CoinGecko 코인 페이지를 북마크할 때만 사용됩니다. 설치하지 않으면 일반 캡처/즐겨찾기는 사용할 수 있지만 Chrome 북마크 자동 추가와 임시코인 버튼은 동작하지 않습니다.

## 웹 UI 사용법

### 목록 새로고침

지정한 페이지 범위의 코인을 모두 읽어 표시하고 `recent-coins-preview.csv`를 갱신합니다. 기본값은 페이지 전체이며, 필요하면 등록 기간을 최근 1~30일로 제한할 수 있습니다. 이 단계는 파일을 다운로드하거나 캡처하지 않습니다.

- **페이지**: 1~10
- **등록 기간**: 페이지 전체(기본값) 또는 최근 1~30일
- **완료된 코인도 다시 수집**: 이미 완료된 항목도 강제로 다시 캡처합니다.

### 원클릭 수집

목록에서 선택한 코인만 처리합니다. 아무것도 선택하지 않으면 현재 목록 전체를 처리합니다.

일반 코인은 **CoinGecko 상세 정보·공식 사이트 스크린샷·백서/PDF를 먼저 수집한 다음** 포트폴리오 즐겨찾기를 추가합니다. 이미 즐겨찾기된 코인도 캡처가 완료되지 않았다면 수집하며, 캡처가 이미 완료된 코인은 파일 중복을 막기 위해 캡처를 건너뛰고 즐겨찾기 상태만 확인합니다. 로그인·추가·확인에 실패한 코인은 실행 로그와 매니페스트에 실패 상태를 남깁니다. 이름 또는 심볼에 `Tokenized`, `xStock`, `Wrapped`, `Bridged`, `Staked`가 포함된 항목은 예외로, 사이트 캡처와 백서/PDF 수집 없이 즐겨찾기만 추가합니다.

스크린샷을 저장하기 전 브라우저의 네트워크 유휴 상태를 최대 5초까지 기다립니다. 이 대기는 무한 스크롤의 끝을 기다리는 방식이 아니며, 이후의 스크롤 기반 지연 로딩 확인도 횟수가 제한되어 있습니다.

CoinGecko 상세 페이지의 접근 제한은 실행을 중단합니다. 외부 웹사이트·SNS·탐색기·백서 링크가 403 또는 접근 확인 화면을 반환하면 이를 검토 상태로 기록하고 우회하지 않으며, 가능한 다른 출처와 다음 코인 처리는 계속합니다.

코인마다 다음 순서로 저장합니다.

1. CoinGecko `Website` 링크 → 공식 사이트 전체 페이지 PNG
2. 웹사이트가 없으면 `Community`의 Twitter/X → 계정 화면 PNG
3. 둘 다 없으면 `Explorers` 링크 → 탐색기 화면 PNG
4. 백서가 실제 PDF면 원본 PDF 저장
5. HTML 백서면 전체 PNG + 브라우저 인쇄 PDF 저장
6. 공식 사이트와 HTML 백서/문서 페이지에 직접 연결된 PDF 첨부파일도 내려받기

완료된 코인은 다음 실행에서 건너뜁니다. `완료된 코인도 다시 수집`을 켜면 다시 처리합니다. CLI에서는 `--since-days 0`이 요청한 페이지 전체를 뜻합니다.

원클릭 수집이 성공하면 캡처 파일이 있고 포트폴리오 상태가 `added` 또는 `already_active`인 코인 페이지를 Chrome 북마크 지정 폴더에 자동 추가합니다. 북마크 확장 프로그램이 설치되지 않았거나 접근할 수 없으면 오류 알림이 표시되며, 확장 프로그램 설치 후 같은 수집을 다시 실행하면 기존 북마크는 중복 없이 건너뜁니다.

### 임시코인 문서 캡처 버튼

**임시코인 문서 캡처 (임시)** 버튼은 Chrome의 `임시코인` 북마크 폴더에서 북마크를 읽어 각 페이지 전체 PNG와 인쇄 PDF를 저장하고, 페이지에 연결된 PDF 첨부파일도 다운로드합니다. 폴더가 없거나 같은 이름의 폴더가 여러 개면 모호한 대상을 임의로 선택하지 않고 오류를 표시합니다. HTTP 오류, 접근 제한, 사라진 페이지는 `skipped_inaccessible`로 기록하고 다음 북마크를 처리합니다. 이 버튼은 북마크 원본을 변경하거나 삭제하지 않습니다.

### 선택 즐겨찾기 추가

목록에서 코인을 선택한 뒤 버튼을 누릅니다. 각 코인의 상세 페이지에서 `Add to Portfolio`를 열고, 가장 최신 포트폴리오 행의 `+`를 눌러 추가한 뒤 별표 활성 상태를 확인합니다. 이미 포트폴리오에 있는 코인은 다시 추가하지 않습니다.

가장 최신 포트폴리오의 실제 보유 코인 수를 확인합니다. 최대 1,000개까지는 해당 폴더를 사용하고, 가득 찼으면 `Add to New Portfolio`로 다음 번호의 폴더를 만들어 사용합니다(예: `16` 다음은 `17`). 폴더 행의 `+` 컨트롤을 찾거나 추가 후 상태를 검증할 수 없으면 성공으로 처리하지 않고 검토 상태로 남깁니다. 이름 또는 심볼에 `Tokenized`, `xStock`, `Wrapped`, `Bridged`, `Staked`가 포함된 항목은 포트폴리오에 추가하되 사이트 캡처와 백서/PDF 수집은 건너뜁니다.

- 이미 활성: `already_active`
- 추가 성공: `added`
- 로그인 필요: `login_required`
- 상태를 확인하지 못함: `portfolio_state_unconfirmed`
- 폴더/`+`를 찾지 못함: `portfolio_folder_not_found`, `portfolio_row_plus_not_found`
- 새 포트폴리오 생성 실패: `portfolio_creation_failed`
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

웹 UI 없이 명령줄로도 실행할 수 있습니다. `--profile`을 지정하지 않으면 사용할 수 있는 실사용 Chrome 디버깅 주소를 자동 감지해 연결합니다. `--attach`는 실사용 Chrome 연결을 필수로 요구하며, 디버깅 주소가 없으면 전용 프로필로 조용히 넘어가지 않고 오류를 냅니다. 이 스크립트의 기본 모드는 파일을 저장하지 않는 `dry-run`입니다. 실제 수집은 `--mode capture`를 명시해야 합니다.

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

열린 전용 브라우저 창에서 직접 로그인한 뒤, 해당 프로필을 `--profile`로 지정해 실행합니다. `--profile`을 지정하지 않으면 사용 가능한 실사용 Chrome 세션에 자동 연결합니다. 웹 UI는 실사용 Chrome 연결을 요구합니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| `Chrome 연결 필요` | `chrome://inspect/#remote-debugging`에서 허용을 켜고 다시 시도 |
| 승인 팝업이 안 뜸 | Allow가 이미 켜져 있으면 정상일 수 있습니다. 실행 로그의 연결 모드와 실제 CDP 연결 결과를 확인 |
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
