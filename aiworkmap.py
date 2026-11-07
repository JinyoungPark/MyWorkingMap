#!/usr/bin/env python3
"""AI 작업 지도 — 백그라운드 상주 프로그램.

  python3 aiworkmap.py run        수집기 + 브라우저 뷰어 (자동 시작에 등록되는 것)
  python3 aiworkmap.py collect    수집기만 (뷰어는 다른 PC에서 볼 때)
  python3 aiworkmap.py serve      뷰어만
  python3 aiworkmap.py snapshot   지금 상태를 HTML 한 장으로 저장
  python3 aiworkmap.py status     기록 위치·저장소 확인

저장소(요약 JSON이 모이는 곳)는 ~/.ai-work-map/config.json 의 "store",
또는 환경변수 AIWM_STORE 로 바꿀 수 있다. 여러 PC가 같은 공유 폴더(예: OneDrive)를
쓰면 뷰어 하나에서 모든 PC의 작업이 합쳐져 보인다.
"""
import argparse
import json
import os
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ai
import logparse as L
import updater as U

APP_DIR = Path(os.environ.get("AIWM_HOME") or Path.home() / ".ai-work-map")
APP_DIR.mkdir(parents=True, exist_ok=True)
LOG = APP_DIR / "agent.log"

# pythonw(Windows 백그라운드)에서는 콘솔이 없어서 출력을 파일로 돌린다
if sys.stderr is None or sys.stdout is None:
    _f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _f


def log(msg):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


CFG = APP_DIR / "config.json"


def config():
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(**kv):
    c = config()
    c.update(kv)
    CFG.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def store_dir():
    path = os.environ.get("AIWM_STORE") or config().get("store")
    p = Path(os.path.expanduser(path)) if path else APP_DIR / "store"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 수집기 ────────────────────────────────────────────────
class Collector:
    def __init__(self, store):
        self.store = store
        self.cache = {}       # 세션 파일 경로 → (서명, 파싱 결과)
        self.proj_sig = {}    # (소스, 프로젝트 폴더) → 서명
        self.team = {}        # 소스 키 → {프로젝트 폴더: [MausBot 봇 세션]}
        self.export_sig = None
        self.last_run = None

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(L.redact(json.dumps(data, ensure_ascii=False)), encoding="utf-8")
        os.replace(tmp, path)

    def once(self):
        changed = 0
        for src in L.find_sources():
            try:
                pdirs = list(L.project_dirs(src["root"]))
            except OSError:
                continue
            for pdir in pdirs:
                try:
                    files = [(f, f.stat()) for f in pdir.glob("*.jsonl")]
                    mem = [(f, f.stat()) for f in (pdir / "memory").glob("*.md")] if (pdir / "memory").is_dir() else []
                except OSError:
                    continue
                sig = tuple(sorted((f.name, st.st_size, int(st.st_mtime)) for f, st in files + mem))
                k = (src["key"], pdir.name)
                if self.proj_sig.get(k) == sig:
                    continue
                sessions = []
                for f, st in files:
                    fsig = (st.st_size, int(st.st_mtime))
                    hit = self.cache.get(str(f))
                    if hit and hit[0] == fsig:
                        s = hit[1]
                    else:
                        try:
                            s = L.parse_session(f)
                        except Exception:
                            log(f"세션 읽기 실패 {f}: {traceback.format_exc(limit=1)}")
                            continue
                        self.cache[str(f)] = (fsig, s)
                    if s:
                        sessions.append(s)
                # MausBot 봇 세션은 '봇 팀'으로 모은다. 봇 전용 폴더에서 일한 것은 팀에만, 내 프로젝트에서 일한 것은 양쪽에
                bots = [s for s in sessions if s.get("bot")]
                team = self.team.setdefault(src["key"], {})
                if bots or pdir.name in team:
                    team[pdir.name] = bots
                    self._write_team(src)
                sessions = [s for s in sessions if not (s.get("bot") or {}).get("home")]
                memory = L.load_memory(pdir)
                if not sessions and not memory:
                    self.proj_sig[k] = sig
                    continue
                sessions.sort(key=lambda s: s["start"] or "", reverse=True)
                cwd = next((s["cwd"] for s in sessions if s["cwd"]), None)
                data = {
                    "key": f"{src['key']}/{pdir.name}",
                    "source": src["label"],
                    "machine": L.machine(),
                    "name": L.project_name(pdir.name, cwd),
                    "updated": datetime.now().isoformat(timespec="seconds"),
                    "memory": memory,
                    "sessions": sessions,
                }
                try:
                    self._write(self.store / src["key"] / f"{pdir.name}.json", data)
                except OSError as e:   # 공유 폴더 동기화 중 잠김 등 → 다음 차례에 다시
                    log(f"저장 실패 {pdir.name}: {e}")
                    continue
                self.proj_sig[k] = sig
                changed += 1
        changed += self._exports()
        self.last_run = datetime.now()
        return changed

    def _team_data(self, key, label, sessions):
        sessions.sort(key=lambda s: s["start"] or "", reverse=True)
        return {
            "key": f"{key}/_mausbot",
            "source": label,
            "machine": L.machine(),
            "name": "🤖 MausBot 봇 팀",
            "team": True,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "memory": [],
            "sessions": sessions,
        }

    def _write_team(self, src):
        sessions = [s for group in self.team.get(src["key"], {}).values() for s in group]
        path = self.store / src["key"] / "_mausbot.json"
        try:
            if sessions:
                self._write(path, self._team_data(src["key"], src["label"], sessions))
            elif path.exists():
                path.unlink()
        except OSError as e:   # 공유 폴더 잠김 등 → 다른 프로젝트 수집은 계속
            log(f"봇 팀 저장 실패: {e}")

    def _exports(self):
        """`openmausbot export` 로 받은 폴더(클라우드 봇 작업 포함)를 읽는다."""
        dirs = L.maus_export_dirs(config().get("mausbot_exports") or [])
        files = [f for d in dirs for f in sorted((d / "transcripts").glob("*.json"))]
        try:
            sig = tuple((str(f), f.stat().st_size, int(f.stat().st_mtime)) for f in files)
        except OSError:
            return 0
        if sig == self.export_sig:
            return 0
        sessions = [s for f in files if (s := L.parse_maus_transcript(f, f.parent.parent.name))]
        key = L._key("MausBot-export")
        path = self.store / key / "_mausbot.json"
        had = path.exists()
        try:
            if sessions:
                self._write(path, self._team_data(key, "MausBot 내보내기", sessions))
            elif had:
                path.unlink()
        except OSError as e:
            log(f"MausBot 내보내기 저장 실패: {e}")
            return 0
        self.export_sig = sig
        return int(bool(sessions) or had)

    def loop(self, interval):
        log(f"수집기 시작 · 저장소 {self.store}")
        while True:
            try:
                n = self.once()
                if n:
                    log(f"프로젝트 {n}개 갱신")
            except Exception:
                log("수집 오류: " + traceback.format_exc(limit=2))
            time.sleep(interval)


# ── 뷰어 ──────────────────────────────────────────────────
class Viewer:
    def __init__(self, store, updater=None):
        self.store = store
        self.lock = threading.Lock()
        self.sig, self.version, self.body = None, 0, b"{}"
        self.demo = False
        self.data = {"projects": []}
        self.token = secrets.token_urlsafe(24)   # 이 화면에서 보낸 요청인지 확인 (다른 웹사이트가 몰래 누르지 못하게)
        self.updater = updater or U.Updater(log=log)
        self.explainer = ai.Explainer(APP_DIR, log=log)
        self.login = ai.Login()

    def merged(self):
        projects = []
        for f in sorted(self.store.glob("*/*.json")):
            try:
                projects.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue   # 다른 PC가 쓰는 중이면 다음 차례에
        projects.sort(key=lambda p: max((s["end"] or "" for s in p["sessions"]), default=""), reverse=True)
        sources = {}
        for p in projects:
            s = sources.setdefault(p["source"] + "@" + p.get("machine", ""), {"label": p["source"], "machine": p.get("machine", ""), "updated": ""})
            s["updated"] = max(s["updated"], p.get("updated", ""))
        return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sources": list(sources.values()), "projects": projects}

    def current(self):
        with self.lock:
            sig = tuple((str(f), f.stat().st_size, f.stat().st_mtime) for f in sorted(self.store.glob("*/*.json")))
            if sig != self.sig:
                data = self.merged()
                self.version += 1
                data["version"] = self.version
                data["app_version"] = U.current_version()
                data["demo"] = self.demo
                self.data = data
                self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.sig = sig
            return self.version, self.body

    def find_session(self, pkey, sid):
        self.current()
        for p in self.data["projects"]:
            if p["key"] == pkey:
                if not sid:
                    return p, None
                for x in p["sessions"]:
                    if x["id"] == sid:
                        return p, x
        return None, None

    def ai_state(self):
        st = ai.status()
        prov = config().get("ai")
        if prov not in st or not st[prov]["loggedIn"]:
            prov = next((k for k, v in st.items() if v["loggedIn"]), None) if prov != "off" else "off"
        return {"provider": prov, "providers": st, "login": self.login.state}

    def post(self, path, body):
        if path == "/update/check":
            return self.updater.check()
        if path == "/update/apply":
            ok, msg = self.updater.apply()
            if ok:
                threading.Timer(1.5, U.restart).start()
            return {"ok": ok, "message": msg}
        if path == "/ai/config":
            prov = body.get("provider")
            if prov in ai.NAMES or prov == "off":
                save_config(ai=prov)
            return self.ai_state()
        if path == "/ai/login":
            self.login.start(body.get("provider"))
            return self.ai_state()
        if path == "/ai/login/code":
            return {"ok": self.login.code(str(body.get("code", "")))}
        if path == "/ai/explain":
            prov = self.ai_state()["provider"]
            if not prov or prov == "off":
                return {"ok": False, "message": "연결된 AI가 없습니다. 위의 ‘AI 연결’에서 로그인하세요."}
            p, sess = self.find_session(body.get("project"), body.get("session"))
            if not p:
                return {"ok": False, "message": "기록을 찾을 수 없습니다."}
            if sess:
                eid, title, turns = f"{p['key']}/{sess['id']}", sess["title"], sess["turns"]
            else:   # 프로젝트 전체: 모든 세션의 요청을 시간 순서로
                eid, title = f"{p['key']}/_project", f"프로젝트 {p['name']}"
                turns = sorted((t for x in p["sessions"] for t in x["turns"]), key=lambda t: t.get("start") or "")
            ok, msg = self.explainer.start(eid, prov, title, turns, p.get("source", ""))
            return {"ok": ok, "message": msg, "id": eid}
        return None

    def handler(self):
        viewer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def _local(self):
                # 주소창에 이 PC 주소로 연 경우만 허용 (DNS 리바인딩 방지)
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost")

            def do_GET(self):
                if not self._local():
                    return self._send(403, b"forbidden", "text/plain")
                path, _, qs = self.path.partition("?")
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                if path == "/":
                    html = (L.HERE / "template.html").read_text(encoding="utf-8")
                    html = html.replace("/*__DATA__*/null", '"LIVE"').replace("/*__TOKEN__*/''", json.dumps(viewer.token))
                    self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/version":
                    self._send(200, str(viewer.current()[0]).encode(), "text/plain")
                elif path == "/data.json":
                    self._send(200, viewer.current()[1], "application/json; charset=utf-8")
                elif path == "/update":
                    self._json(viewer.updater.snapshot())
                elif path == "/ai":
                    self._json(viewer.ai_state())
                elif path == "/ai/explain":
                    from urllib.parse import unquote
                    self._json(viewer.explainer.get(unquote(q.get("id", ""))))
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if not self._local() or self.headers.get("X-AIWM-Token") != viewer.token:
                    return self._send(403, b"forbidden", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    body = {}
                try:
                    res = viewer.post(self.path.split("?")[0], body)
                except Exception as e:
                    log("요청 처리 오류: " + traceback.format_exc(limit=2))
                    return self._json({"ok": False, "message": str(e)}, 500)
                if res is None:
                    return self._send(404, b"not found", "text/plain")
                self._json(res)
        return H

    def serve(self, port):
        # 외부에서 접근하지 못하게 이 PC(127.0.0.1)에서만 연다
        srv = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        log(f"뷰어 시작 · http://127.0.0.1:{port}/")
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="AI 작업 지도")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "collect", "serve", "snapshot", "status", "demo"])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=5, help="수집 간격(초)")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--out", default=str(L.HERE / "ai-work-map.html"))
    a = ap.parse_args()

    if a.cmd == "demo":
        # 예시 데이터로 뷰어만 띄운다 (실제 기록·설정은 건드리지 않음)
        import tempfile
        import demo
        global APP_DIR
        APP_DIR = Path(tempfile.gettempdir()) / "aiworkmap-demo"
        v = Viewer(demo.write(APP_DIR))
        v.demo = True
        if not a.no_open:
            threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{a.port}/",)).start()
        v.serve(a.port)
        return

    store = store_dir()

    if a.cmd == "status":
        print(f"버전: {U.current_version()}")
        print(f"저장소: {store}")
        for s in L.find_sources():
            print(f"기록 위치 [{s['label']}] {s['root']}")
        for d in L.maus_export_dirs(config().get("mausbot_exports") or []):
            print(f"MausBot 내보내기 {d}")
        for f in sorted(store.glob("*/*.json")):
            print(f"  요약: {f.relative_to(store)}")
        return

    if a.cmd == "snapshot":
        Collector(store).once()
        data = Viewer(store).merged()
        tpl = (L.HERE / "template.html").read_text(encoding="utf-8")
        Path(a.out).write_text(tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")
        print(f"저장: {a.out}")
        if not a.no_open:
            webbrowser.open(Path(a.out).resolve().as_uri())
        return

    if a.cmd in ("run", "collect"):
        c = Collector(store)
        c.once()   # 뷰어가 처음부터 내용을 보여 주도록 한 번 먼저 수집
        if a.cmd == "collect":
            c.loop(a.interval)
            return
        threading.Thread(target=c.loop, args=(a.interval,), daemon=True).start()

    upd = U.Updater(log=log)
    threading.Thread(target=upd.loop, daemon=True).start()
    try:
        if not a.no_open:
            threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{a.port}/",)).start()
        Viewer(store, upd).serve(a.port)
    except OSError as e:
        # 이미 다른 뷰어가 떠 있으면 수집만 계속한다 (자동 재시작 반복 방지)
        log(f"뷰어 포트 {a.port} 사용 중({e}) → 수집은 계속하고, 포트가 비면 뷰어를 엽니다")
        if a.cmd == "run":
            while True:
                time.sleep(30)
                try:
                    Viewer(store, upd).serve(a.port)
                except OSError:
                    continue
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
