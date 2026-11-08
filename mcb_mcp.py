#!/usr/bin/env python3
"""MyCollaboBot — 봇에게 붙여 주는 도구 (MCP, 표준입출력).

봇(claude/codex)이 이 도구를 부르면 AI 작업 지도 앱(127.0.0.1)에 물어보고 답을 돌려준다.
  approve      파일 수정·명령 실행 전에 사용자 허락 받기 (claude --permission-prompt-tool)
  ask_user     사용자에게 질문하고 답 기다리기
  list_bots    함께 일하는 봇 목록
  send_to_bot  다른 봇에게 일을 맡기고 결과 받기
앱 주소·열쇠·실행 번호는 환경변수(MCB_URL, MCB_KEY, MCB_RUN)로 받는다.
"""
import json
import os
import sys
import urllib.request

URL, KEY, RUN = os.environ.get("MCB_URL", ""), os.environ.get("MCB_KEY", ""), os.environ.get("MCB_RUN", "")

TOOLS = [
    {"name": "approve", "description": "도구를 쓰기 전에 사용자에게 허락을 받는다 (앱이 자동으로 부름).",
     "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}},
                     "required": ["tool_name", "input"]}},
    {"name": "ask_user", "description": "사용자에게 질문하고 답을 기다린다. 정말 사람이 정해야 할 때만 쓴다.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string", "description": "짧고 분명한 질문"}},
                     "required": ["question"]}},
    {"name": "list_bots", "description": "함께 일하는 다른 봇들의 이름과 역할을 본다.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send_to_bot", "description": "다른 봇에게 일을 맡기고, 그 봇이 끝낼 때까지 기다려 결과를 받는다.",
     "inputSchema": {"type": "object", "properties": {"bot": {"type": "string", "description": "봇 이름"},
                                                       "message": {"type": "string", "description": "맡길 일. 그 봇이 이것만 보고 알 수 있게 자세히"}},
                     "required": ["bot", "message"]}},
]


def call_app(tool, args):
    req = urllib.request.Request(f"{URL}/mcb/{tool}", method="POST",
                                 data=json.dumps({"run": RUN, **args}, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-MCB-Key": KEY})
    with urllib.request.urlopen(req, timeout=4 * 3600) as r:   # 사람이 답할 때까지 기다린다
        return json.loads(r.read())


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        mid, meth = m.get("id"), m.get("method")
        if mid is None:
            continue   # 알림(notifications/…)은 답하지 않는다
        if meth == "initialize":
            ver = (m.get("params") or {}).get("protocolVersion") or "2024-11-05"
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                                            "serverInfo": {"name": "mcb", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif meth == "tools/call":
            p = m.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            try:
                res = call_app(name, args)
                text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                if name != "approve" and isinstance(res, dict) and "text" in res:
                    text = res["text"]
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as e:
                if name == "approve":   # 앱과 연결이 끊기면 안전하게 거절
                    text = json.dumps({"behavior": "deny", "message": f"허락을 받을 수 없습니다: {e}"}, ensure_ascii=False)
                    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
                else:
                    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"실패: {e}"}], "isError": True}})
        elif meth == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()
