"""MyCollaboBot — 내 구독 계정으로 봇 팀을 직접 돌린다.

봇마다 이름·역할·작업 폴더·허락 방식·예약이 있고, 일을 맡기면 이 PC의 claude/codex CLI를 띄운다.
  허락 요청   claude가 파일 수정·명령 실행 전에 mcb_mcp.approve 를 부르면 화면에 허락/거절 카드가 뜬다
  봇끼리 협업 send_to_bot 으로 다른 봇에게 일을 넘기고 결과를 받는다 (깊이 3, 되돌아오는 고리 금지)
  예약 실행   매일·평일 정해진 시각, 또는 N분마다
봇 정의와 실행 기록은 ~/.ai-work-map/bots 에 둔다. 봇이 한 세션은 Claude Code 기록에도 남아 작업 지도에 보인다.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import ai
import logparse as L

HERE = Path(__file__).resolve().parent
APPROVAL = {"ask": "모두 물어보기", "edits": "파일 수정은 자동, 명령은 물어보기", "readonly": "읽기만 (수정·명령 금지)"}
MAX_DEPTH = 3
WAIT = 30 * 60            # 허락·질문을 기다리는 시간 (지나면 거절)
KEEP_RUNS = 300
MCB_TOOLS = ["mcp__mcb__ask_user", "mcp__mcb__list_bots", "mcp__mcb__send_to_bot",
             "WebSearch", "WebFetch"]   # 웹 읽기는 허락 없이 ('읽기만' 봇도 조사할 수 있게)
MAX_TASKS = 4             # 팀장이 한 번에 맡기는 일 (팀원들은 동시에 일한다)
MAX_ROUNDS = 3            # 팀원에게 맡기는 차수 (검토 뒤 다시 맡기기 포함)
AUTO_RULE = "사용자는 일을 던지고 최종 결과만 보고 판단한다. 사용자에게 되묻지 말고, 모자란 정보는 합리적으로 가정한 뒤 그 가정을 밝히고 끝까지 진행한다."
TASK_FORMAT = ("```팀작업\n[{\"봇\": \"팀원 이름\", \"일\": \"맡길 일 (그 팀원이 혼자 할 수 있게 구체적으로)\"}]\n```\n"
               "팀에 맞는 팀원이 없으면 임시 팀원을 만들어 맡길 수 있다: {\"봇\": \"새 이름\", \"역할\": \"그 팀원의 역할\", \"일\": \"맡길 일\"} (팀장과 같은 AI, 읽기만).")


def now():
    return datetime.now().isoformat(timespec="seconds")


# 내 문서 검색(MyDocSearch)이 남긴 mcp-launch.json 이 있으면 봇마다 그 찾기·읽기 도구를 붙인다 (MyCollaboratorBots와 같은 방식)
MDS = "mydocsearch"
MDS_TOOLS = ["search_documents", "read_document", "list_folders"]
DOCS_RULE = ("내 문서 검색 도구(search_documents·read_document)로 사용자의 PC 문서를 찾아 읽을 수 있다. 문서를 근거로 답할 때는 "
             "문장 바로 뒤에 [파일명 · 쪽](doc:문서ID#c조각번호)로 밝히고, 문서에 없는 내용은 지어내지 않는다.")


def mds_dir():
    if os.environ.get("MCB_MDS_DATA"):
        return Path(os.environ["MCB_MDS_DATA"])
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MyDocSearch"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "MyDocSearch"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "MyDocSearch"


def mds_launch():
    """문서 검색 MCP 서버를 띄우는 방법 {command, args, env} — 실행 파일이 없으면(앱을 옮김·지움) None"""
    try:
        l = json.loads((mds_dir() / "mcp-launch.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(l, dict) or not l.get("command") or not isinstance(l.get("args"), list) or not Path(l["command"]).exists():
        return None
    return {"command": l["command"], "args": [str(a) for a in l["args"]], "env": {**(l.get("env") or {}), "MDS_TOOLS": "basic"}}


def _python():
    """봇 도구(mcb_mcp)를 띄울 파이썬. Windows 백그라운드(pythonw)는 표준입출력이 없어 python.exe로."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def _slug(name):
    s = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", name).strip("-")[:30]
    return s or "bot"


class Engine:
    def __init__(self, app_dir, log=print):
        self.dir = Path(app_dir) / "bots"
        (self.dir / "runs").mkdir(parents=True, exist_ok=True)
        (self.dir / "jobs").mkdir(parents=True, exist_ok=True)
        self.log = log
        self.lock = threading.RLock()
        self.key = secrets.token_urlsafe(24)    # 봇 도구(mcb_mcp) → 앱 요청 확인용
        self.url = None                         # 뷰어가 열리면 채워진다
        self.procs = {}                         # 실행 id → Popen
        self.pending = {}                       # 대기 id → 허락/질문
        self.changed = time.time()
        self.bots = self._load_bots()
        self.runs = self._load_runs()
        self.teams = self._load_teams()
        self.jobs = self._load_jobs()
        self.tq = {}            # 팀 id → 기다리는 일 id들
        self.team_busy = {}     # 팀 id → 지금 하는 일 id

    # ── 저장 ──
    def _load_bots(self):
        try:
            return json.loads((self.dir / "bots.json").read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_bots(self):
        tmp = self.dir / "bots.tmp"
        tmp.write_text(json.dumps(self.bots, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "bots.json")
        self.changed = time.time()

    def _load_runs(self):
        runs = {}
        for f in sorted((self.dir / "runs").glob("*.json"))[-KEEP_RUNS:]:
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if r.get("status") in ("running", "waiting", "queued"):
                r["status"], r["ended"] = "stopped", r.get("ended") or now()
                r["error"] = "앱이 다시 시작되어 멈췄습니다."
            runs[r["id"]] = r
        return runs

    def _save_run(self, r):
        r["events"] = r["events"][-400:]
        tmp = self.dir / "runs" / f"{r['id']}.tmp"
        tmp.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / "runs" / f"{r['id']}.json")
        self.changed = time.time()

    def _tag_session(self, sid, bot):
        """봇이 만든 Claude Code 세션을 작업 지도 수집기가 알아보게 적어 둔다."""
        f = self.dir / "sessions.json"
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            m = {}
        m[sid] = {"name": bot["name"], "id": bot["id"]}
        tmp = self.dir / "sessions.tmp"
        tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)

    # ── 봇 ──
    def bot(self, key):
        return next((b for b in self.bots if b["id"] == key or b["name"] == key), None)

    def save_bot(self, d):
        name = str(d.get("name") or "").strip()[:30]
        if not name:
            raise ValueError("봇 이름을 적어 주세요.")
        with self.lock:
            b = self.bot(d.get("id") or "") if d.get("id") else None
            if any(x["name"] == name and x is not b for x in self.bots):
                raise ValueError(f"'{name}' 봇이 이미 있습니다.")
            if not b:
                bid = _slug(name)
                while self.bot(bid):
                    bid += "-" + secrets.token_hex(2)
                b = {"id": bid, "created": now(), "last_fired": None, "session": None}
                self.bots.append(b)
            engine = d.get("engine") if d.get("engine") in ai.NAMES else "claude"
            workdir = os.path.expanduser(str(d.get("workdir") or "").strip()) or str(self.dir / b["id"] / "work")
            if b.get("workdir") and b["workdir"] != workdir or b.get("engine") not in (None, engine):
                b["session"] = None   # 폴더·AI가 바뀌면 이전 대화는 이어 갈 수 없다
            sch = d.get("schedule") or {}
            b.update({
                "name": name,
                "role": str(d.get("role") or "")[:4000],
                "engine": engine,
                "workdir": workdir,
                "approval": d.get("approval") if d.get("approval") in APPROVAL else "ask",
                "continue": bool(d.get("continue", True)),
                "schedule": {"kind": sch.get("kind") if sch.get("kind") in ("off", "daily", "weekdays", "interval") else "off",
                             "time": str(sch.get("time") or "09:00")[:5],
                             "minutes": max(10, int(sch.get("minutes") or 60)),
                             "task": str(sch.get("task") or "")[:4000]},
            })
            Path(workdir).mkdir(parents=True, exist_ok=True)
            self._save_bots()
            return b

    def delete_bot(self, bid):
        self.delete_bot_check(bid)
        with self.lock:
            self.bots = [b for b in self.bots if b["id"] != bid]
            self._save_bots()

    # ── 실행 ──
    def start(self, bot_key, task, origin="user", parent=None, job=None, extra="", label=None):
        """봇 하나에게 일을 맡긴다. bot_key가 dict면 팀장이 만든 임시 팀원. label은 화면에 보일 짧은 일 이름."""
        b = bot_key if isinstance(bot_key, dict) else self.bot(bot_key)
        if not b:
            raise ValueError(f"'{bot_key}' 봇이 없습니다.")
        task = str(task or "").strip()
        if not task:
            raise ValueError("맡길 일을 적어 주세요.")
        if not self.url:
            raise RuntimeError("뷰어가 아직 열리지 않았습니다.")
        if not ai.find(b["engine"]):
            raise RuntimeError(f"{ai.NAMES[b['engine']]} 프로그램을 찾을 수 없습니다.")
        with self.lock:
            if any(r["bot"] == b["id"] and r["status"] in ("running", "waiting") for r in self.runs.values()):
                raise RuntimeError(f"{b['name']} 봇은 지금 다른 일을 하는 중입니다.")
            chain = []
            p = self.runs.get(parent) if parent else None
            while p:
                chain.append(p["bot"])
                p = self.runs.get(p.get("parent"))
            if len(chain) >= MAX_DEPTH:
                raise RuntimeError(f"봇끼리 일을 넘기는 단계가 너무 깊습니다 (최대 {MAX_DEPTH}).")
            if b["id"] in chain:
                raise RuntimeError(f"{b['name']} 봇에게 다시 돌아가는 일 넘기기는 할 수 없습니다.")
            rid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            r = {"id": rid, "bot": b["id"], "bot_name": b["name"], "engine": b["engine"], "task": label or task, "prompt": task,
                 "origin": origin, "parent": parent, "children": [], "status": "running", "started": now(), "ended": None,
                 "session": None, "result": "", "error": "", "events": [], "steps": {}, "job": job, "temp": bool(b.get("temp")), "extra": extra}
            self.runs[rid] = r
            if parent and parent in self.runs:
                self.runs[parent]["children"].append(rid)
            self._save_run(r)
        threading.Thread(target=self._run, args=(r, b), daemon=True).start()
        return r

    def stop(self, rid):
        p = self.procs.get(rid)
        r = self.runs.get(rid)
        if r and r["status"] in ("running", "waiting"):
            r["error"] = "사용자가 멈췄습니다."
            r["status"] = "stopped"
        for pid, w in list(self.pending.items()):
            if w["run"] == rid:
                self._resolve(pid, {"allow": False, "text": "작업이 멈췄습니다."})
        if p and p.poll() is None:
            p.terminate()
        return bool(r)

    def _ev(self, r, kind, **kv):
        e = {"t": now(), "kind": kind, **kv}
        r["events"].append(e)
        if kind == "tool":
            r["steps"][kv["cat"]] = r["steps"].get(kv["cat"], 0) + 1
        self.changed = time.time()
        return e

    def _mcp_conf(self, r):
        conf = {"mcpServers": {"mcb": {"command": _python(), "args": [str(HERE / "mcb_mcp.py")],
                                       "env": {"MCB_URL": self.url, "MCB_KEY": self.key, "MCB_RUN": r["id"]}}}}
        docs = mds_launch()
        if docs:
            conf["mcpServers"][MDS] = docs
        f = self.dir / "runs" / f"{r['id']}.mcp.json"
        f.write_text(json.dumps(conf), encoding="utf-8")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return f

    def _prompt(self, b, r):
        team = ", ".join(f"{x['name']}({cut(x['role'], 40)})" for x in self.bots if x["id"] != b["id"]) or "없음"
        lines = [f"너는 MyCollaboBot 팀의 '{b['name']}' 봇이다."]
        if b["role"]:
            lines.append(f"역할: {b['role']}")
        lines.append(f"함께 일하는 봇: {team}. 다른 봇이 더 잘할 일은 mcb 도구 send_to_bot 으로 맡기고, 꼭 사람이 정해야 할 것만 ask_user 로 묻는다.")
        if r["origin"].startswith("bot:"):
            lines.append(f"이 일은 '{r['origin'][4:]}' 봇이 맡겼다. 끝나면 결과를 짧고 분명하게 정리해 답한다.")
        if mds_launch():
            lines.append(DOCS_RULE)
        if r.get("extra"):
            lines.append(r["extra"])
        lines.append("마지막 답은 한국어로, 무엇을 했고 결과가 무엇인지 몇 줄로 정리한다.")
        return "\n".join(lines)

    def _run(self, r, b):
        exe = ai.find(b["engine"])
        try:
            Path(b["workdir"]).mkdir(parents=True, exist_ok=True)
            mcp = self._mcp_conf(r)
            if b["engine"] == "claude":
                args = ["-p", "--output-format", "stream-json", "--verbose",
                        "--append-system-prompt", self._prompt(b, r),
                        "--mcp-config", str(mcp), "--strict-mcp-config", "--allowedTools", *MCB_TOOLS,
                        *([f"mcp__{MDS}__{t}" for t in MDS_TOOLS] if mds_launch() else [])]
                if b["approval"] == "readonly":
                    args += ["--permission-prompts", "none"]
                else:
                    args += ["--permission-prompt-tool", "mcp__mcb__approve"]
                    if b["approval"] == "edits":
                        args += ["--permission-mode", "acceptEdits"]
                if b.get("continue") and b.get("session") and r["origin"] == "user":
                    args += ["--resume", b["session"]]
                stdin = r.get("prompt") or r["task"]
            else:
                q = lambda s: json.dumps(s, ensure_ascii=False)
                env_t = "{" + ",".join(f"{k}={q(v)}" for k, v in (("MCB_URL", self.url), ("MCB_KEY", self.key), ("MCB_RUN", r["id"]))) + "}"
                args = ["exec", "--json", "--skip-git-repo-check",
                        "--sandbox", "read-only" if b["approval"] == "readonly" else "workspace-write",
                        "-C", b["workdir"],
                        "-c", f"mcp_servers.mcb.command={q(_python())}",
                        "-c", f"mcp_servers.mcb.args=[{q(str(HERE / 'mcb_mcp.py'))}]",
                        "-c", f"mcp_servers.mcb.env={env_t}",
                        # 봇 도구(질문·팀원·일 넘기기)는 codex가 따로 승인을 묻지 않게: 도구 자체가 사람에게 묻는다
                        "-c", 'mcp_servers.mcb.default_tools_approval_mode="approve"',
                        "-c", f"mcp_servers.mcb.tool_timeout_sec={WAIT * 8}", "-"]   # 다른 봇·사람을 기다리는 동안 끊기지 않게
                docs = mds_launch()
                if docs:
                    base = f"mcp_servers.{MDS}"
                    args[-1:-1] = ["-c", f"{base}.command={q(docs['command'])}", "-c", f"{base}.args=[{','.join(q(a) for a in docs['args'])}]",
                                   "-c", base + ".env={" + ",".join(f"{k}={q(v)}" for k, v in docs["env"].items()) + "}",
                                   "-c", f'{base}.default_tools_approval_mode="approve"']
                stdin = self._prompt(b, r) + "\n\n" + (r.get("prompt") or r["task"])
            p = subprocess.Popen([exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", errors="replace", cwd=b["workdir"],
                                 shell=ai.IS_WIN and exe.lower().endswith(".cmd"), creationflags=ai.NOWIN,
                                 env={**os.environ, "NO_COLOR": "1", "MCP_TOOL_TIMEOUT": str(WAIT * 8 * 1000)})
            self.procs[r["id"]] = p
            p.stdin.write(stdin)
            p.stdin.close()
            err = []
            threading.Thread(target=lambda: err.extend(p.stderr.readlines()[-20:]), daemon=True).start()
            feed = self._claude_line if b["engine"] == "claude" else self._codex_line
            tools = {}
            for line in p.stdout:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    feed(r, b, o, tools)
            p.wait()
            with self.lock:
                if r["status"] in ("running", "waiting"):
                    if p.returncode == 0 and not r["error"]:
                        r["status"] = "done"
                    else:
                        r["status"] = "error"
                        r["error"] = r["error"] or ("".join(err).strip()[-400:] or f"종료 코드 {p.returncode}")
        except Exception as e:
            r["status"], r["error"] = "error", str(e)
        finally:
            self.procs.pop(r["id"], None)
            try:
                (self.dir / "runs" / f"{r['id']}.mcp.json").unlink()
            except OSError:
                pass
            with self.lock:
                r["ended"] = now()
                if not r["result"] and r["error"]:
                    r["result"] = ""
                self._ev(r, "end", status=r["status"])
                self._save_run(r)
            self.log(f"봇 {b['name']} 작업 {r['status']} · {r['id']}")

    def _claude_line(self, r, b, o, tools):
        t = o.get("type")
        if t == "system" and o.get("subtype") == "init" and o.get("session_id"):
            r["session"] = o["session_id"]
            b["session"] = o["session_id"]
            self._save_bots()
            self._tag_session(o["session_id"], b)
        elif t == "assistant":
            for c in (o.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    self._ev(r, "text", text=c["text"].strip()[:1500])
                elif c.get("type") == "tool_use":
                    name, inp = c.get("name", ""), c.get("input") or {}
                    if name == "mcp__mcb__approve":
                        continue
                    e = self._ev(r, "tool", cat=L.classify(name, inp), tool=name.split("__")[-1], detail=L.detail(name, inp) or "")
                    tools[c.get("id")] = e
        elif t == "user":
            for c in (o.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") and c.get("tool_use_id") in tools:
                    tools[c["tool_use_id"]]["error"] = True
        elif t == "result":
            r["result"] = str(o.get("result") or "")[:6000]
            if o.get("is_error"):
                r["error"] = r["result"][:400] or str(o.get("subtype"))
            self._save_run(r)

    def _codex_line(self, r, b, o, tools):
        t = o.get("type", "")
        it = o.get("item") or {}
        k = it.get("type") or it.get("item_type") or ""
        if t == "thread.started":
            r["session"] = o.get("thread_id")
        elif t == "item.completed" and k in ("agent_message", "assistant_message"):
            r["result"] = str(it.get("text") or "")[:6000]
            self._ev(r, "text", text=r["result"][:1500])
        elif t == "item.completed" and k == "command_execution":
            cmd = it.get("command") or ""
            e = self._ev(r, "tool", cat=L.classify("Bash", {"command": cmd}), tool="Bash", detail=cut(cmd, 90))
            if it.get("exit_code") not in (0, None):
                e["error"] = True
        elif t == "item.completed" and k == "file_change":
            for ch in it.get("changes") or []:
                self._ev(r, "tool", cat="edit", tool="Edit", detail=os.path.basename(str(ch.get("path") or "")))
        elif t == "item.completed" and k == "mcp_tool_call":
            name = f"mcp__{it.get('server')}__{it.get('tool')}"
            self._ev(r, "tool", cat=L.classify(name, {}), tool=str(it.get("tool")), detail="")
        elif t == "item.completed" and k == "web_search":
            self._ev(r, "tool", cat="web", tool="WebSearch", detail=cut(str(it.get("query") or ""), 90))
        elif t in ("turn.failed", "error"):
            r["error"] = str((o.get("error") or {}).get("message") if isinstance(o.get("error"), dict) else o.get("message") or o)[:400]

    # ── 봇 도구(mcb_mcp)에서 오는 요청 — 사람이 답할 때까지 이 스레드가 기다린다 ──
    def _wait(self, r, kind, **kv):
        pid = secrets.token_hex(6)
        w = {"id": pid, "run": r["id"], "bot": r["bot_name"], "kind": kind, "created": now(), "event": threading.Event(), "answer": None, **kv}
        with self.lock:
            self.pending[pid] = w
            r["status"] = "waiting"
            self._ev(r, kind, pending=pid, **{k: v for k, v in kv.items() if k in ("tool", "detail", "question", "cat")})
        w["event"].wait(WAIT)
        with self.lock:
            self.pending.pop(pid, None)
            if r["status"] == "waiting":
                r["status"] = "running"
            ans = w["answer"] or {"allow": False, "text": "답이 없어 거절했습니다."}
            for e in r["events"]:
                if e.get("pending") == pid:
                    e["answer"] = ans
            self.changed = time.time()
        return ans

    def _resolve(self, pid, ans):
        w = self.pending.get(pid)
        if not w:
            return False
        w["answer"] = ans
        w["event"].set()
        return True

    def answer(self, pid, allow=None, text=""):
        return self._resolve(pid, {"allow": bool(allow) if allow is not None else True, "text": str(text or "")[:4000]})

    def mcb(self, tool, body):
        r = self.runs.get(body.get("run"))
        if not r:
            return {"text": "알 수 없는 실행입니다."} if tool != "approve" else {"behavior": "deny", "message": "알 수 없는 실행"}
        if tool == "approve":
            name, inp = body.get("tool_name", ""), body.get("input") or {}
            ans = self._wait(r, "approve", tool=name.split("__")[-1], cat=L.classify(name, inp), detail=L.detail(name, inp) or cut(json.dumps(inp, ensure_ascii=False), 200),
                             full=cut(json.dumps(inp, ensure_ascii=False), 1500))
            if ans["allow"]:
                return {"behavior": "allow", "updatedInput": inp}
            return {"behavior": "deny", "message": ans.get("text") or "사용자가 거절했습니다."}
        if tool == "ask_user":
            ans = self._wait(r, "ask", question=str(body.get("question") or "")[:1000])
            return {"text": ans.get("text") or "(답 없음)"}
        if tool == "list_bots":
            me = r["bot"]
            return {"text": "\n".join(f"- {b['name']}: {b['role'] or '(역할 없음)'}" for b in self.bots if b["id"] != me) or "다른 봇이 없습니다."}
        if tool == "send_to_bot":
            try:
                child = self.start(str(body.get("bot") or ""), body.get("message"), origin=f"bot:{r['bot_name']}", parent=r["id"])
            except Exception as e:
                return {"text": f"맡기지 못했습니다: {e}"}
            with self.lock:
                self._ev(r, "handoff", to=child["bot_name"], child=child["id"], text=cut(child["task"], 200))
            while child["status"] in ("running", "waiting") and r["status"] in ("running", "waiting"):
                time.sleep(1)
            if child["status"] in ("running", "waiting"):
                self.stop(child["id"])
            res = child["result"] or child["error"] or "(결과 없음)"
            return {"text": f"[{child['bot_name']} 봇 결과 · {child['status']}]\n{res}"}
        return {"text": "알 수 없는 도구"}

    # ── 팀: 팀장이 알아서 나눠 맡기고 검토해 최종 결과를 낸다 (MyCollaboratorBots와 같은 진행) ──
    def _load_teams(self):
        try:
            return json.loads((self.dir / "teams.json").read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_teams(self):
        tmp = self.dir / "teams.tmp"
        tmp.write_text(json.dumps(self.teams, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "teams.json")
        self.changed = time.time()

    def _load_jobs(self):
        jobs = {}
        for f in sorted((self.dir / "jobs").glob("*.json"))[-KEEP_RUNS:]:
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("status") in ("queued", "running"):
                j["status"], j["ended"] = "stopped", j.get("ended") or now()
                j["error"] = "앱이 다시 시작되어 멈췄습니다."
            jobs[j["id"]] = j
        return jobs

    def _save_job(self, j):
        with self.lock:    # 팀원들이 동시에 끝나며 같은 파일을 쓸 수 있다
            tmp = self.dir / "jobs" / f"{j['id']}.tmp"
            tmp.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.dir / "jobs" / f"{j['id']}.json")
            self.changed = time.time()

    def team(self, key):
        return next((t for t in self.teams if t["id"] == key or t["name"] == key), None)

    def save_team(self, d):
        name = str(d.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("팀 이름을 적어 주세요.")
        lead = self.bot(str(d.get("lead") or ""))
        if not lead:
            raise ValueError("팀장 봇을 고르세요.")
        members = []
        for m in d.get("members") or []:
            b = self.bot(str(m))
            if b and b["id"] != lead["id"] and b["id"] not in members:
                members.append(b["id"])
        with self.lock:
            t = self.team(d.get("id") or "") if d.get("id") else None
            if any(x["name"] == name and x is not t for x in self.teams):
                raise ValueError(f"'{name}' 팀이 이미 있습니다.")
            if not t:
                tid = "t-" + _slug(name)
                while self.team(tid):
                    tid += "-" + secrets.token_hex(2)
                t = {"id": tid, "created": now(), "last_fired": None}
                self.teams.append(t)
            sch = d.get("schedule") or {}
            t.update({"name": name, "lead": lead["id"], "members": members,
                      "schedule": {"kind": sch.get("kind") if sch.get("kind") in ("off", "daily", "weekdays", "interval") else "off",
                                   "time": str(sch.get("time") or "09:00")[:5], "minutes": max(10, int(sch.get("minutes") or 60)),
                                   "task": str(sch.get("task") or "")[:4000]}})
            self._save_teams()
            return t

    def delete_team(self, tid):
        with self.lock:
            if self.team_busy.get(tid):
                raise RuntimeError("일하는 중인 팀입니다. 멈춘 뒤 지우세요.")
            self.teams = [t for t in self.teams if t["id"] != tid]
            self._save_teams()

    def delete_bot_check(self, bid):
        for t in self.teams:
            if t["lead"] == bid or bid in t["members"]:
                raise RuntimeError(f"'{t['name']}' 팀에 들어 있습니다. 팀에서 먼저 빼세요.")

    def example_team(self):
        """예시 팀: 팀장·조사·정리·검토 봇과 '예시 팀'. 같은 이름의 봇이 있으면 그 봇을 쓴다."""
        made = []
        for name, role, approval in (
                ("팀장", "일을 나눠 맡기고 결과를 검토해 최종 결과를 낸다", "ask"),
                ("조사", "자료를 찾아 읽고 사실과 출처를 정리한다", "readonly"),
                ("정리", "내용을 읽기 쉬운 글·표로 정리한다", "readonly"),
                ("검토", "결과에서 빠지거나 틀린 점, 앞뒤가 맞지 않는 점을 찾는다", "readonly")):
            b = self.bot(name) or self.save_bot({"name": name, "role": role, "engine": "claude", "approval": approval, "continue": False})
            made.append(b["id"])
        name = "예시 팀"
        while self.team(name):
            name += "+"
        return self.save_team({"name": name, "lead": made[0], "members": made[1:]})

    def send_team(self, tid, text, trigger="user"):
        t = self.team(tid)
        if not t:
            raise ValueError("팀을 찾지 못했습니다.")
        text = str(text or "").strip()
        if not text:
            raise ValueError("맡길 일을 적어 주세요.")
        if not self.url:
            raise RuntimeError("뷰어가 아직 열리지 않았습니다.")
        jid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        j = {"id": jid, "team": t["id"], "team_name": t["name"], "text": text[:8000], "trigger": trigger, "status": "queued",
             "started": now(), "ended": None, "stages": [], "final": "", "note": "", "error": "", "stop": False}
        with self.lock:
            self.jobs[jid] = j
            self._save_job(j)
            self.tq.setdefault(t["id"], []).append(jid)
        self._pump(t["id"])
        return j

    def _pump(self, tid):
        with self.lock:
            if self.team_busy.get(tid) or not self.tq.get(tid):
                return
            jid = self.tq[tid].pop(0)
            self.team_busy[tid] = jid
        threading.Thread(target=self._work_guard, args=(self.jobs[jid],), daemon=True).start()

    def _work_guard(self, j):
        try:
            if j["status"] == "queued":
                j["status"] = "running"
                j["started"] = now()
                self._save_job(j)
                self._work(j)
        except Exception as e:
            j["status"], j["error"] = "error", str(e)
            self.log(f"팀 작업 오류 {j['team_name']}: {e}")
        finally:
            if j["status"] == "running":
                j["status"] = "stopped" if j["stop"] else "error"
            j["ended"] = j["ended"] or now()
            self._save_job(j)
            with self.lock:
                self.team_busy.pop(j["team"], None)
            self._pump(j["team"])

    def stop_team(self, jid):
        j = self.jobs.get(jid)
        if not j:
            return False
        with self.lock:
            if j["status"] == "queued":
                j["status"], j["ended"] = "stopped", now()
                q = self.tq.get(j["team"], [])
                if jid in q:
                    q.remove(jid)
                self._save_job(j)
                return True
        j["stop"] = True
        for st in j["stages"]:
            if st.get("run") and self.runs.get(st["run"], {}).get("status") in ("running", "waiting"):
                self.stop(st["run"])
        return True

    def _history(self, j):
        past = [x for x in sorted(self.jobs.values(), key=lambda x: x["started"]) if x["team"] == j["team"] and x["id"] != j["id"] and x["started"] <= j["started"]][-5:]
        out = []
        for x in past:
            out.append(f"{'예약 작업' if x['trigger'] == 'schedule' else '사용자'}: {x['text'][:1500]}")
            if x["final"]:
                out.append(f"팀장 최종 결과: {x['final'][:1500]}")
        return "\n".join(out)

    def _stage(self, j, b, step, rnd, task, prompt, extra, origin):
        st = {"step": step, "round": rnd, "bot": b["id"], "bot_name": b["name"], "temp": bool(b.get("temp")), "task": task[:600],
              "run": None, "status": "running", "text": "", "started": now(), "ended": None}
        j["stages"].append(st)
        self._save_job(j)
        try:
            r = self.start(b, prompt, origin=origin, job=j["id"], extra=extra, label=f"[{j['team_name']}] {task}"[:300])
        except Exception as e:
            st.update(status="error", text=f"(맡기지 못함: {e})", ended=now())
            self._save_job(j)
            return st
        st["run"] = r["id"]
        self._save_job(j)
        while r["status"] in ("running", "waiting"):
            if j["stop"]:
                self.stop(r["id"])
            time.sleep(1)
        st.update(status=r["status"], text=(r["result"] or r["error"] or "(빈 답)")[:12000], ended=now())
        self._save_job(j)
        return st

    def _pick(self, items, team, lead):
        """팀장이 이름으로 고른 팀원. 없는 이름에 역할을 주면 이번 일에만 쓰는 임시 팀원 (팀장과 같은 AI, 읽기만)."""
        temps, out = {}, []
        for it in items:
            if not isinstance(it, dict):
                continue
            n = str(it.get("봇") or it.get("bot") or "").strip()[:30]
            task = str(it.get("일") or it.get("task") or "").strip()
            role = str(it.get("역할") or it.get("role") or "").strip()
            b = next((x for x in team if x["name"] == n), None) or next((x for x in team if n and x["name"] in n), None)
            if not b and n and role and n != lead["name"]:
                b = temps.setdefault(n, {"id": "tmp-" + _slug(n) + "-" + secrets.token_hex(2), "temp": True, "name": n, "role": role[:1000],
                                         "engine": lead["engine"], "approval": "readonly", "workdir": lead["workdir"], "continue": False, "session": None})
            if b and task and all(o[0]["id"] != b["id"] for o in out):   # 한 차수에 봇 하나는 한 가지 일
                out.append((b, task))
        return out[:MAX_TASKS]

    def _work(self, j):
        t = self.team(j["team"])
        if not t:
            raise RuntimeError("팀을 찾지 못했습니다.")
        lead = self.bot(t["lead"])
        if not lead:
            raise RuntimeError("팀장 봇이 없습니다. 팀 설정을 확인하세요.")
        team = [b for b in (self.bot(m) for m in t["members"]) if b]
        asker = "예약 작업" if j["trigger"] == "schedule" else "사용자"
        roster = ("팀원:\n" + "\n".join(f"- {b['name']}: {b['role'] or '(역할 없음)'}" for b in team)) if team else "정해 둔 팀원은 없습니다."
        lead_rule = f"너는 '{t['name']}' 팀의 팀장이다. {roster}\n\n{AUTO_RULE}\n팀 작업 중에는 send_to_bot 대신 아래 '팀작업' 블록으로 맡긴다."
        past = self._history(j)

        def lead_stage(step, rnd, label, prompt):
            return self._stage(j, lead, step, rnd, label, prompt, lead_rule, f"team:{t['name']}")

        # 1) 팀장: 직접 답하거나 일을 나눈다
        plan = lead_stage("plan", 0, "계획", f"{'지금까지의 대화:' + chr(10) + past + chr(10) * 2 if past else ''}{asker}의 요청: {j['text']}\n\n"
                          f"나눠 맡기면 좋은 일이면 짧게 계획을 말하고 답 끝에 다음 블록을 붙인다 (최대 {MAX_TASKS}개, 맡은 팀원들은 동시에 일한다):\n{TASK_FORMAT}\n"
                          "간단한 일이면 나누지 말고 바로 최종 결과를 답한다.")
        if j["stop"] or plan["status"] != "done":
            return self._fail(j, plan)
        text, items = take_block(plan["text"], "팀작업")
        plan["text"] = text or "팀원에게 나눠 맡깁니다."
        assigned = self._pick(items, team, lead)
        if not assigned:
            return self._final(j, plan, text)
        done = []
        for rnd in range(1, MAX_ROUNDS + 1):
            # 2) 팀원들이 동시에
            res = [None] * len(assigned)

            def one(i, b, task, rnd=rnd):
                res[i] = self._stage(j, b, "task", rnd, task, f"팀장 '{lead['name']}'이 맡긴 일: {task}\n\n({asker}의 원래 요청: {j['text']})",
                                     AUTO_RULE, f"bot:{lead['name']}")
            th = [threading.Thread(target=one, args=(i, b, task), daemon=True) for i, (b, task) in enumerate(assigned)]
            for x in th:
                x.start()
            for x in th:
                x.join()
            if j["stop"]:
                return
            done += [s for s in res if s]
            # 3) 팀장이 검토: 더 맡기거나 최종 결과
            last = rnd >= MAX_ROUNDS
            report = "\n\n".join(f"## {s['round']}차 · {s['bot_name']} — {s['task']}\n{s['text'][:8000]}" for s in done)
            rev = lead_stage("review", rnd, f"{rnd}차 검토", f"{asker}의 요청: {j['text']}\n\n팀원들의 결과:\n\n{report}\n\n" +
                             ("이제 최종 결과를 쓴다. 더 나눠 맡기지 않는다." if last else
                              f"결과를 검토한다. 빠졌거나 틀렸거나 서로 맞지 않는 부분을 채우려면 왜 필요한지 한두 줄로 말하고 다시 맡긴다 (남은 차수 {MAX_ROUNDS - rnd}번):\n{TASK_FORMAT}\n"
                              "충분하면 블록 없이 최종 결과를 쓴다.") +
                             "\n최종 결과는 사용자가 바로 판단할 수 있게: 맨 앞에 한두 줄 결론, 그다음 근거와 내용, 끝에 확인이 필요한 점.")
            if j["stop"] or rev["status"] != "done":
                return self._fail(j, rev)
            text, items = take_block(rev["text"], "팀작업")
            rev["text"] = text or "더 맡깁니다."
            assigned = [] if last else self._pick(items, team, lead)
            if not assigned:
                return self._final(j, rev, text)

    def _fail(self, j, st):
        if j["stop"]:
            j["status"] = "stopped"
        else:
            j["status"], j["error"] = "error", st["text"][:400]
        j["ended"] = now()
        self._save_job(j)

    def _final(self, j, st, text):
        st["final"] = True
        j["final"] = text or st["text"]
        j["status"], j["ended"] = "wait", now()     # 사용자의 판단(승인·수정 요청)을 기다린다
        self._save_job(j)
        _notify(f"MyCollaboBot · {j['team_name']}", "예약 작업을 마쳤습니다. 결과를 확인하세요." if j["trigger"] == "schedule" else "결과가 나왔습니다. 확인하고 판단해 주세요.")

    def decide(self, jid, verdict, note=""):
        """approve — 승인으로 끝, revise — 의견을 붙여 팀장이 이어서 고친다"""
        j = self.jobs.get(jid)
        if not j or j["status"] != "wait":
            raise ValueError("판단할 결과를 찾지 못했습니다 (이미 판단했을 수 있습니다).")
        if verdict == "approve":
            j["status"], j["decided"] = "approved", now()
            self._save_job(j)
            return j
        note = str(note or "").strip()
        if not note:
            raise ValueError("무엇을 고칠지 적어 주세요.")
        j["status"], j["note"], j["decided"] = "revise", note[:2000], now()
        self._save_job(j)
        return self.send_team(j["team"], f"수정 요청: {note}\n\n(바로 앞의 최종 결과를 이 의견대로 고쳐 다시 내 주세요.)")

    # ── 예약 ──
    def due(self, b, t):
        s = b.get("schedule") or {}
        if s.get("kind") in (None, "off") or not s.get("task"):
            return False
        last = datetime.fromisoformat(b["last_fired"]) if b.get("last_fired") else None
        if s["kind"] == "interval":
            return not last or t - last >= timedelta(minutes=s["minutes"])
        if s["kind"] == "weekdays" and t.weekday() >= 5:
            return False
        try:
            hh, mm = map(int, s["time"].split(":"))
        except ValueError:
            return False
        at = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return t >= at and (not last or last < at) and t - at < timedelta(hours=1)

    def schedule_loop(self):
        while True:
            time.sleep(20)
            t = datetime.now()
            for tm in list(self.teams):
                try:
                    if self.due(tm, t):
                        tm["last_fired"] = t.isoformat(timespec="seconds")
                        self._save_teams()
                        self.send_team(tm["id"], tm["schedule"]["task"], trigger="schedule")
                        self.log(f"팀 예약 실행: {tm['name']}")
                except Exception as e:
                    self.log(f"팀 예약 실행 실패 {tm['name']}: {e}")
            for b in list(self.bots):
                try:
                    if self.due(b, t):
                        b["last_fired"] = t.isoformat(timespec="seconds")
                        self._save_bots()
                        self.start(b["id"], b["schedule"]["task"], origin="schedule")
                        self.log(f"예약 실행: {b['name']}")
                except Exception as e:
                    self.log(f"예약 실행 실패 {b['name']}: {e}")

    def next_fire(self, b):
        s = b.get("schedule") or {}
        if s.get("kind") in (None, "off") or not s.get("task"):
            return None
        t = datetime.now()
        if s["kind"] == "interval":
            last = datetime.fromisoformat(b["last_fired"]) if b.get("last_fired") else t
            return max(t, last + timedelta(minutes=s["minutes"])).isoformat(timespec="minutes")
        try:
            hh, mm = map(int, s["time"].split(":"))
        except ValueError:
            return None
        at = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while at <= t or (s["kind"] == "weekdays" and at.weekday() >= 5):
            at += timedelta(days=1)
        return at.isoformat(timespec="minutes")

    # ── 화면용 ──
    def snapshot(self):
        with self.lock:
            runs = sorted(self.runs.values(), key=lambda r: r["started"], reverse=True)[:80]
            lite = [{k: v for k, v in r.items() if k not in ("events", "prompt", "extra")} | {"last": next((e for e in reversed(r["events"]) if e["kind"] in ("tool", "text", "handoff")), None)}
                    for r in runs]
            jobs = sorted(self.jobs.values(), key=lambda j: j["started"], reverse=True)[:60]
            return {
                "teams": [{**t, "next": self.next_fire(t), "busy": self.team_busy.get(t["id"]), "queued": len(self.tq.get(t["id"], []))} for t in self.teams],
                "jobs": [{**j, "stages": [{**st, "text": cut(st["text"], 300)} for st in j["stages"]]} for j in jobs],
                "docs": bool(mds_launch()),
                "max": {"tasks": MAX_TASKS, "rounds": MAX_ROUNDS},
                "bots": [{**b, "next": self.next_fire(b)} for b in self.bots],
                "runs": lite,
                "pending": [{k: v for k, v in w.items() if k != "event"} for w in self.pending.values()],
                "engines": {k: {"name": v["name"], "ready": v["installed"] and v["loggedIn"]} for k, v in ai.status().items()},
                "approval": APPROVAL,
                "changed": self.changed,
            }

    def run_detail(self, rid):
        with self.lock:
            r = self.runs.get(rid)
            return json.loads(json.dumps(r, ensure_ascii=False)) if r else None


def take_block(text, tag):
    """답에서 ```tag [...]``` 블록을 꺼낸다 → (블록을 뺀 글, 항목들)"""
    items = []

    def grab(m):
        try:
            v = json.loads(m.group(1).strip())
            items.extend(v if isinstance(v, list) else [v])
        except ValueError:
            pass    # 형식이 틀림 — 무시
        return ""
    rest = re.sub(r"```\s*" + re.escape(tag) + r"\s*\n([\s\S]*?)```", grab, str(text or "")).strip()
    return rest, items


def _notify(title, msg):
    try:
        import updater
        threading.Thread(target=updater._os_notify, args=(title, msg), daemon=True).start()
    except Exception:
        pass


def cut(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"
