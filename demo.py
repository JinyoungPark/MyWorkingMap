"""예시 데이터: 실제 기록 없이 화면을 둘러보거나 안내 자료용 화면을 찍을 때 쓴다.

  python3 aiworkmap.py demo
가상의 '팀 할 일 앱'(Mac)과 '데이터 정리 스크립트'(WSL) 작업 기록, AI 해설 하나를 만든다.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path


def _t(minutes_ago):
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def S(cat, detail, tool="", error=False, sub=None):
    st = {"cat": cat, "tool": tool or cat, "detail": detail}
    if error:
        st["error"] = True
    if sub:
        st["sub"] = sub
    return st


def turn(ago, dur, prompt, steps, recap, plan=None, interrupted=False):
    return {"prompt": prompt, "start": _t(ago), "end": _t(max(ago - dur, 0)), "steps": steps,
            "reply": "", "recap": recap, "plan": plan or [], "interrupted": interrupted}


def todo_app():
    turns = [
        turn(300, 25, "팀에서 같이 쓰는 할 일 앱을 웹으로 만들고 싶어. 간단하게 시작하자", [
            S("plan", "계획"), S("read", "package.json"), S("read", "폴더 구조 살펴보기"),
            S("edit", "index.html"), S("edit", "app.js"), S("edit", "style.css"), S("edit", "server.js"),
            S("run", "필요한 패키지 설치"), S("run", "개발 서버 켜기"), S("web", "화면 열어 확인")],
            "할 일 추가·완료·삭제가 되는 첫 화면을 만들었습니다. 개발 서버에서 바로 써 볼 수 있습니다.",
            ["화면 뼈대 만들기", "할 일 저장(서버)", "브라우저에서 확인"]),
        turn(260, 30, "로그인 기능 넣어줘. 팀원마다 자기 할 일이 보이게", [
            S("plan", "계획"), S("read", "server.js"), S("edit", "auth.js"), S("edit", "server.js"),
            S("test", "로그인 테스트 실행", error=True), S("read", "오류 내용 확인"), S("edit", "auth.js"),
            S("test", "로그인 테스트 다시 실행"), S("share", "결과 보여 주기")],
            "팀원별 로그인을 넣었습니다. 처음 테스트에서 세션 만료 오류가 나서 고쳤고, 지금은 테스트 8개가 모두 통과합니다.",
            ["로그인 화면", "비밀번호 안전하게 저장", "내 할 일만 보이기", "테스트"]),
        turn(200, 15, "디자인을 더 깔끔하게 해줘", [
            S("read", "style.css"), S("edit", "style.css"), S("edit", "index.html"),
            S("web", "화면 캡처"), S("web", "모바일 크기로 확인"), S("ask", "색 조합 고르기", tool="AskUserQuestion")],
            "여백과 글자 크기를 정리하고, 파란색과 회색 두 가지 색으로 줄였습니다."),
        turn(160, 20, "보안에 문제 없는지 점검해줘", [
            S("plan", "계획"),
            S("agent", "보안 점검 (로그인·저장·입력값)", tool="Agent",
              sub={"type": "general-purpose", "desc": "보안 점검: 로그인, 저장, 입력값", "finished": True,
                   "tools": {"read": 14, "run": 6, "test": 2}}),
            S("edit", "auth.js"), S("edit", "server.js"), S("test", "보안 테스트")],
            "입력값을 걸러 주지 않던 곳 2군데를 고쳤습니다. 비밀번호는 안전하게 저장되고 있습니다.",
            ["점검 맡기기", "찾은 문제 고치기", "다시 확인"]),
        turn(110, 25, "윈도우 팀원도 쓸 수 있게 배포 파일 만들어줘", [
            S("read", "배포 방법 확인"), S("edit", "build.js"), S("run", "윈도우용 묶기"),
            S("run", "맥용 묶기"), S("test", "묶은 앱 실행 확인"), S("git", "v1.0.0 릴리스 올리기")],
            "맥과 윈도우용 설치 파일을 만들어 v1.0.0으로 올렸습니다."),
        turn(40, 8, "마감일이 다가오면 알림이 오게 해줘", [
            S("plan", "계획"), S("read", "app.js"), S("edit", "notify.js"), S("edit", "app.js"),
            S("test", "알림 테스트")],
            "마감 하루 전 알림을 만드는 중입니다. 화면 알림은 됐고, 메일 알림이 남았습니다.",
            ["알림 시간 계산", "화면 알림", "메일 알림"]),
        turn(6, 2, "팀즈로도 알림 보낼 수 있을까?", [
            S("web", "Teams 웹훅 방법 찾아보기"), S("read", "notify.js")],
            "", interrupted=False),
    ]
    files = ["~/Developer/team-todo/" + f for f in ["index.html", "app.js", "style.css", "server.js", "auth.js", "build.js", "notify.js"]]
    s1 = {"id": "demo-todo-1", "title": "팀 할 일 앱 만들기", "cwd": "~/Developer/team-todo",
          "start": turns[0]["start"], "end": turns[-1]["end"], "turns": turns, "subagents": 1, "files": files}
    return {"key": "Mac_demo/-Users-me-Developer-team-todo", "source": "Mac", "machine": "my-mac",
            "name": "~/Developer/team-todo", "updated": _t(0), "sessions": [s1],
            "memory": [
                {"name": "user-prefers-korean", "type": "user", "desc": "사용자는 한국어 설명과 그림 위주의 정리를 좋아한다",
                 "body": "설명은 짧게, 화면이나 도형으로 보여 준다."},
                {"name": "team-todo-goal", "type": "project", "desc": "팀 할 일 앱: 맥·윈도우 팀원 모두 사용, 연말까지 알림 기능",
                 "body": "Why: 팀 업무가 메신저에 흩어져 있어서.\nHow to apply: 기능을 늘리기보다 쓰기 쉬움을 먼저."},
                {"name": "no-secrets-in-repo", "type": "feedback", "desc": "비밀번호·키를 저장소에 넣지 말 것",
                 "body": "설정 파일 예시만 올리고 실제 값은 각자 PC에 둔다."}]}


def pipeline():
    turns = [
        turn(1500, 30, "엑셀 보고서 여러 개를 하나로 합치는 스크립트 만들어줘", [
            S("read", "보고서 폴더 살펴보기"), S("edit", "merge.py"), S("run", "합치기 실행"), S("test", "결과 확인")],
            "폴더의 엑셀 파일 12개를 한 장으로 합치는 merge.py를 만들었습니다."),
        turn(1400, 20, "매주 월요일 아침에 자동으로 돌게 해줘", [
            S("edit", "crontab 설정"), S("run", "예약 등록"), S("run", "시험 실행")],
            "매주 월요일 8시에 자동 실행되도록 예약했습니다."),
        turn(1300, 15, "결과를 팀 공유 폴더에 올려줘", [
            S("edit", "merge.py"), S("run", "공유 폴더 올리기", error=True), S("read", "권한 확인"), S("edit", "merge.py"), S("run", "다시 올리기")],
            "처음엔 공유 폴더 권한 오류가 났고, 경로를 고쳐 올렸습니다."),
    ]
    s = {"id": "demo-pipe-1", "title": "주간 보고서 합치기 자동화", "cwd": "~/work/report-merge",
         "start": turns[0]["start"], "end": turns[-1]["end"], "turns": turns, "subagents": 0,
         "files": ["~/work/report-merge/merge.py"]}
    return {"key": "WSL_Ubuntu_demo/-home-me-work-report-merge", "source": "WSL·Ubuntu", "machine": "my-pc",
            "name": "~/work/report-merge", "updated": _t(0), "sessions": [s], "memory": []}


VISUAL = {
    "summary": "팀 할 일 웹앱을 만들고 로그인·보안·배포까지 마친 뒤 알림을 붙이는 중",
    "goal": "팀 전체가 쓰는 할 일 앱",
    "phases": [
        {"title": "첫 화면", "icon": "🧱", "status": "done", "what": "할 일 추가·완료·삭제", "requests": [1]},
        {"title": "로그인", "icon": "🔐", "status": "done", "what": "팀원별 할 일, 테스트 통과", "requests": [2]},
        {"title": "디자인 정리", "icon": "🎨", "status": "done", "what": "여백·색을 단순하게", "requests": [3]},
        {"title": "보안 점검", "icon": "🛡️", "status": "done", "what": "입력값 문제 2곳 고침", "requests": [4]},
        {"title": "배포 v1.0.0", "icon": "📦", "status": "done", "what": "맥·윈도우 설치 파일", "requests": [5]},
        {"title": "마감 알림", "icon": "⏰", "status": "doing", "what": "화면 알림 됨, 메일 남음", "requests": [6]},
        {"title": "팀즈 알림", "icon": "💬", "status": "todo", "what": "웹훅으로 보내기 검토", "requests": [7]},
    ],
    "direction": {"from": "간단한 할 일 목록", "to": "알림 주는 팀 협업 도구", "changed": True,
                  "why": "배포 후 팀원 요청으로 알림 쪽으로 확장"},
    "next": ["메일 알림 마무리", "팀즈 웹훅 시험", "v1.1 배포"],
    "risks": ["메일 발송 설정 미확인", "알림 테스트 1개뿐"],
}


def write(home):
    home = Path(home)
    store = home / "store"
    for p in (todo_app(), pipeline()):
        f = store / p["key"].split("/")[0] / (p["key"].split("/")[1] + ".json")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    eid = "Mac_demo/-Users-me-Developer-team-todo/demo-todo-1"
    aidir = home / "ai"
    aidir.mkdir(parents=True, exist_ok=True)
    (aidir / (re.sub(r"[^A-Za-z0-9._-]+", "_", eid)[:150] + ".json")).write_text(json.dumps(
        {"text": json.dumps(VISUAL, ensure_ascii=False), "visual": VISUAL, "provider": "claude", "turns": 7,
         "at": _t(1), "seconds": 41}, ensure_ascii=False), encoding="utf-8")
    return store
