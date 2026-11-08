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
MCB_TOOLS = ["mcp__mcb__ask_user", "mcp__mcb__list_bots", "mcp__mcb__send_to_bot"]


def now():
    return datetime.now().isoformat(timespec="seconds")


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
        self.log = log
        self.lock = threading.RLock()
        self.key = secrets.token_urlsafe(24)    # 봇 도구(mcb_mcp) → 앱 요청 확인용
        self.url = None                         # 뷰어가 열리면 채워진다
        self.procs = {}                         # 실행 id → Popen
        self.pending = {}                       # 대기 id → 허락/질문
        self.changed = time.time()
        self.bots = self._load_bots()
        self.runs = self._load_runs()

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
        with self.lock:
            self.bots = [b for b in self.bots if b["id"] != bid]
            self._save_bots()

    # ── 실행 ──
    def start(self, bot_key, task, origin="user", parent=None):
        b = self.bot(bot_key)
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
            r = {"id": rid, "bot": b["id"], "bot_name": b["name"], "engine": b["engine"], "task": task, "origin": origin,
                 "parent": parent, "children": [], "status": "running", "started": now(), "ended": None,
                 "session": None, "result": "", "error": "", "events": [], "steps": {}}
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
                        "--mcp-config", str(mcp), "--strict-mcp-config", "--allowedTools", *MCB_TOOLS]
                if b["approval"] == "readonly":
                    args += ["--permission-prompts", "none"]
                else:
                    args += ["--permission-prompt-tool", "mcp__mcb__approve"]
                    if b["approval"] == "edits":
                        args += ["--permission-mode", "acceptEdits"]
                if b.get("continue") and b.get("session") and r["origin"] == "user":
                    args += ["--resume", b["session"]]
                stdin = r["task"]
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
                stdin = self._prompt(b, r) + "\n\n" + r["task"]
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
            lite = [{k: v for k, v in r.items() if k != "events"} | {"last": next((e for e in reversed(r["events"]) if e["kind"] in ("tool", "text", "handoff")), None)}
                    for r in runs]
            return {
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


def cut(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"
