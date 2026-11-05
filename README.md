# MyWorkingMap — AI 작업 지도

Claude Code가 남긴 작업 기록을 읽어서 **AI가 무엇을 했는지, 내가 어느 방향으로 가고 있는지**를
플로차트와 도형으로 보여 줍니다. PC마다 백그라운드에 상주하며 실시간으로 갱신합니다.

## 받기
[Releases](https://github.com/JinyoungPark/MyWorkingMap/releases)에서 zip을 받아 원하는 곳(예: `~/Apps/MyWorkingMap`)에 풉니다.
Python 3.9 이상만 있으면 되고, 따로 설치할 패키지는 없습니다.

## 설치 (PC마다 한 번)
| PC | 할 일 |
|---|---|
| Mac | `install/mac-install.command` 더블클릭 |
| Windows (+WSL) | `install/windows-install.cmd` 더블클릭. Python이 없으면 먼저 `winget install Python.Python.3.12` |

설치하면 로그인할 때마다 자동으로 실행되고, 바탕화면에 **AI 작업 지도** 바로가기가 생깁니다.
화면 주소는 `http://127.0.0.1:8765/` 입니다. 해제하려면 같은 폴더의 `*-uninstall`을 실행합니다.

> Windows에서는 **WSL 안에 따로 설치하지 마세요.** Windows 쪽 프로그램이 켜져 있는 WSL 배포판의 기록까지 함께 읽습니다.
> 꺼진 WSL은 건드리지 않으므로, WSL을 억지로 깨워 두지 않습니다.

## 동작 방식
```
기록 폴더 (.claude/projects)  ──▶ 수집기 (5초마다, 바뀐 것만) ──▶ 요약 JSON (비밀값 가림)
  Mac / Windows / WSL                                              │
                                                                   ▼
                                               뷰어 (브라우저, 3초마다 갱신)
```
- 요약 저장소 기본값은 `~/.ai-work-map/store` 입니다 (이 PC 기록만 보임).
- **여러 PC 기록을 한 화면에서 보려면** 모든 PC의 `~/.ai-work-map/config.json`에 같은 공유 폴더를 적습니다.
  ```json
  { "store": "~/OneDrive - 회사이름/AI작업지도" }
  ```
  각 PC가 자기 요약을 그 폴더에 쓰고, 어느 PC의 뷰어에서든 전부 합쳐 보입니다.
- 요약에 들어가는 것은 요청 문장, 작업 단계 이름, AI의 보고 문장, 수정한 파일 이름뿐입니다. 파일 내용과 명령 출력은 들어가지 않습니다.
  API 키, 토큰, 비밀번호처럼 보이는 문자열은 `[가림]`으로 바꿉니다.

## 화면 구성
| 화면 | 보여 주는 것 |
|---|---|
| 🧭 지금 가고 있는 방향 | 최근 요청 8개를 화살표로 잇고, 떠오르는 주제, 방향 전환, 지금 목표, 현재 계획, AI의 마지막 보고를 보여 줍니다 |
| 🔷 도형 타임라인 | 요청마다 카드 하나. 누르면 그 요청의 플로차트가 펼쳐집니다 |
| 🧭 세션 플로차트 | 요청 → 결과가 이어지는 전체 흐름 (최근 60개) |
| 📁 프로젝트 개요 | 세션 목록과 AI가 기억하고 있는 내용(memory) |

기록마다 🍎 Mac, 🪟 Windows, 🐧 WSL 표시가 붙습니다.

## 🤖 AI 해설 — 내 구독 계정 사용
API 키가 필요 없습니다. 이 PC에 **구독 계정으로 로그인된 공식 프로그램**을 불러 씁니다 (MyDocSearch와 같은 방식).

| AI | 쓰는 프로그램 | 계정 |
|---|---|---|
| Claude | Claude Code (`claude -p`) — Claude 데스크톱 앱에 들어 있는 것도 찾아 씀 | claude.ai Pro / Max |
| ChatGPT | Codex (`codex exec`) — ChatGPT 데스크톱 앱에 들어 있는 것도 찾아 씀 | ChatGPT Plus / Pro |

- 화면 오른쪽 위 **⚙️ 설정 → AI 연결**에서 고르고, 로그인이 안 되어 있으면 **로그인** 버튼을 누릅니다 (브라우저가 열림).
- 세션이나 프로젝트 화면의 **🤖 AI 해설 만들기**를 누르면, AI가 기록을 읽고 **단계 도형, 상태(✅ 🔄 ❌ ⏳), 방향 화살표, 다음 할 일, 주의할 점**을 그림으로 정리합니다.
  글 대신 데이터(JSON)로 받아서 앱이 직접 그리기 때문에, 매번 같은 모양으로 나옵니다.
- AI에게는 파일, 명령, 웹 도구를 하나도 주지 않고 빈 폴더에서 실행합니다. 해설 만들기는 작업 기록에 남지 않습니다.
- 버튼을 누를 때만 실행되며, 구독 사용량을 씁니다. 결과는 `~/.ai-work-map/ai`에 보관합니다.

## ⬆️ 업데이트
- 6시간마다 GitHub 릴리스를 확인합니다. 새 버전이 있으면 **화면 위쪽에 알림 띠**가 뜨고, Mac과 Windows에는 시스템 알림도 보냅니다.
- **지금 업데이트**를 누르면 새 버전을 받아 덮어쓰고 스스로 다시 시작합니다. git으로 받은 폴더는 `git pull`을 씁니다.
- 새 버전 내보내기(관리자): `python3 tools/release.py 0.2.0 "바뀐 점"` — VERSION, 태그, GitHub 릴리스를 한 번에 처리합니다.

## 명령 (필요할 때만)
```
python3 aiworkmap.py status     # 어디서 기록을 읽고 어디에 저장하는지
python3 aiworkmap.py snapshot   # 지금 상태를 HTML 한 장으로 저장 (공유용)
python3 aiworkmap.py run        # 수집기 + 뷰어 직접 실행
```
로그: `~/.ai-work-map/agent.log`
