#!/usr/bin/env python3
"""AI 작업 지도: Claude Code 세션 기록(.claude/projects)을 읽어 요약하는 부분.

기록 위치 찾기(Mac, Windows, WSL), 세션 파싱, 비밀값 가리기를 맡는다.
"""
import json
import os
import platform
import re
import socket
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ── 도구 분류 ────────────────────────────────────────────
READ_CMDS = ("cat ", "ls", "grep ", "rg ", "find ", "head ", "tail ", "sed -n", "wc ", "tree", "pwd", "file ", "stat ", "du ", "less ")


def classify(name, inp):
    n = name
    if n in ("Read", "Grep", "Glob", "LS", "NotebookRead", "ToolSearch") or "search" in n.lower() or n.endswith("__read_document"):
        return "read"
    if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "edit"
    if n == "Bash":
        cmd = (inp.get("command") or "").strip()
        if cmd.startswith("git commit") or cmd.startswith("git push") or " git push" in cmd or "gh pr" in cmd or "gh release" in cmd:
            return "git"
        if cmd.startswith(READ_CMDS) or cmd.startswith("cd ") and any(c in cmd for c in ("&& ls", "&& cat", "&& grep")):
            return "read"
        if "test" in cmd or "pytest" in cmd:
            return "test"
        return "run"
    if n in ("Monitor", "TaskStop") or n.startswith("mcp__terminal__"):
        return "run"
    if n in ("WebFetch", "WebSearch") or "Browser" in n or "claude-in-chrome" in n:
        return "web"
    if n in ("Agent", "Task", "Workflow", "SendMessage") or "start_session" in n:
        return "agent"
    if n in ("TodoWrite", "EnterPlanMode", "ExitPlanMode") or n.endswith("update_plan"):
        return "plan"
    if n == "AskUserQuestion":
        return "ask"
    if n in ("Artifact", "SendUserFile") or n.endswith("show_pane"):
        return "share"
    if n.endswith("wrap_up"):
        return "wrap"
    return "other"


def detail(name, inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "file_path", "path", "pattern", "url", "query", "title", "prompt", "command"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            if key in ("file_path", "path"):
                return os.path.basename(v.rstrip("/")) or v
            return v.strip().splitlines()[0][:90]
    return name.split("__")[-1]


def clean_prompt(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", "[붙여넣은 내용]", text, flags=re.S)
    m = re.search(r"<command-name>(.*?)</command-name>", text)
    if m:
        return m.group(1).strip()
    if text.lstrip().startswith(("<task-notification>", "<local-command", "<side-sessions-event>", "<ci-monitor-event>")):
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


# ── 세션 파서 ────────────────────────────────────────────
def iter_records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            # api-request 같은 큰 줄은 type이 맨 앞에 있으므로 파싱 전에 건너뛴다
            if line.startswith('{"type":"') and not line.startswith(('{"type":"user"', '{"type":"assistant"', '{"type":"custom-title"')):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def count_tools(path):
    counts = {}
    for d in iter_records(path):
        if d.get("type") != "assistant":
            continue
        for c in d.get("message", {}).get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                k = classify(c.get("name", ""), c.get("input") or {})
                counts[k] = counts.get(k, 0) + 1
    return counts


def load_subagents(session_dir):
    out = {}
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return out
    for meta in sub.glob("*.meta.json"):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        jl = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
        info = {
            "type": m.get("agentType", "agent"),
            "desc": m.get("description", ""),
            "finished": m.get("finished"),
            "tools": count_tools(jl) if jl.exists() else {},
        }
        if m.get("toolUseId"):
            out[m["toolUseId"]] = info
    return out


def parse_session(path):
    sid = path.stem
    session_dir = path.with_suffix("")
    subagents = load_subagents(session_dir)
    title = None
    tf = session_dir / "custom-title.json"
    if tf.exists():
        try:
            title = json.loads(tf.read_text(encoding="utf-8")).get("customTitle")
        except Exception:
            pass

    turns, cur, tool_index = [], None, {}
    cwd = None
    files_changed = set()

    def new_turn(prompt, ts):
        return {"prompt": prompt, "start": ts, "end": ts, "steps": [], "reply": "", "recap": "", "plan": [], "interrupted": False}

    for d in iter_records(path):
        t = d.get("type")
        if t == "custom-title" and not title:
            title = d.get("customTitle")
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        ts = d.get("timestamp")
        cwd = cwd or d.get("cwd")
        msg = d.get("message") or {}
        content = msg.get("content")

        if t == "user":
            if d.get("isMeta"):
                continue
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        st = tool_index.get(c.get("tool_use_id"))
                        if st is not None and c.get("is_error"):
                            st["error"] = True
                    elif c.get("type") == "text":
                        texts.append(c.get("text", ""))
            raw = "\n".join(texts)
            if "[Request interrupted" in raw:
                if cur:
                    cur["interrupted"] = True
                continue
            prompt = clean_prompt(raw)
            if prompt:
                cur = new_turn(prompt[:600], ts)
                turns.append(cur)
            continue

        # assistant
        if cur is None:
            cur = new_turn("(이어서 진행)", ts)
            turns.append(cur)
        cur["end"] = ts or cur["end"]
        for c in content or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                cur["reply"] = c["text"].strip()[:700]
            elif c.get("type") == "tool_use":
                name, inp = c.get("name", ""), c.get("input") or {}
                cat = classify(name, inp)
                if cat == "wrap":
                    cur["recap"] = (inp.get("recap") or "")[:500]
                    continue
                if cat == "plan":
                    items = inp.get("items") or inp.get("todos") or []
                    if items:
                        cur["plan"] = [str(i.get("content", ""))[:80] for i in items if isinstance(i, dict)][:12]
                if cat == "edit" and inp.get("file_path"):
                    files_changed.add(inp["file_path"])
                step = {"cat": cat, "tool": name.split("__")[-1], "detail": detail(name, inp)}
                if cat == "agent":
                    sa = subagents.get(c.get("id"))
                    if sa:
                        step["sub"] = sa
                tool_index[c.get("id")] = step
                cur["steps"].append(step)

    turns = [x for x in turns if x["steps"] or x["reply"] or x["recap"]]
    if not turns:
        return None
    return {
        "id": sid,
        "title": title or turns[0]["prompt"][:50],
        "cwd": cwd,
        "start": turns[0]["start"],
        "end": turns[-1]["end"],
        "turns": turns,
        "subagents": len(subagents),
        "files": sorted(files_changed)[:200],
    }


def project_name(dirname, cwd):
    if cwd:
        # 어느 PC의 기록이든 사용자 홈은 ~ 로 줄인다 (Mac /Users/x, Linux·WSL /home/x, Windows C:\\Users\\x)
        return re.sub(r"^(/Users/[^/]+|/home/[^/]+|[A-Za-z]:\\Users\\[^\\]+)", "~", cwd)
    return dirname


def load_memory(proj_dir):
    mem = proj_dir / "memory"
    out = []
    if not mem.is_dir():
        return out
    for f in sorted(mem.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        name, desc, mtype = f.stem, "", ""
        m = re.search(r"^description:\s*(.+)$", txt, re.M)
        if m:
            desc = m.group(1).strip()
        m = re.search(r"^\s*type:\s*(\w+)", txt, re.M)
        if m:
            mtype = m.group(1)
        body = re.sub(r"^---.*?---\s*", "", txt, flags=re.S).strip()
        out.append({"name": name, "desc": desc, "type": mtype, "body": body[:600]})
    return out


def project_dirs(root):
    for pdir in sorted(root.iterdir()):
        if not pdir.is_dir():
            continue
        if pdir.name.startswith("-private-tmp") or pdir.name.startswith("-tmp") or "scratch-workspaces" in pdir.name:
            continue
        yield pdir


# ── 기록 위치 찾기 ────────────────────────────────────────
def _is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _running_wsl_distros():
    """실행 중인 WSL 배포판만 돌려준다. 꺼진 배포판을 건드리면 WSL이 깨어나서 계속 켜져 있게 된다."""
    try:
        r = subprocess.run(["wsl.exe", "-l", "--running", "-q"], capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    text = r.stdout.decode("utf-16-le", errors="ignore").replace("\x00", "")
    return [d.strip() for d in text.splitlines() if d.strip() and not d.strip().startswith("docker-desktop")]


def machine():
    return socket.gethostname().split(".")[0]


def _key(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{label}@{machine()}")


def find_sources():
    """이 PC에서 읽을 수 있는 Claude Code 기록 폴더 목록."""
    out = []
    sysname = platform.system()
    if _is_wsl():
        label = "WSL·" + os.environ.get("WSL_DISTRO_NAME", "Linux")
    else:
        label = {"Darwin": "Mac", "Windows": "Windows"}.get(sysname, "Linux")
    home = Path.home() / ".claude" / "projects"
    if home.is_dir():
        out.append({"label": label, "root": home, "key": _key(label)})
    if sysname == "Windows":
        for distro in _running_wsl_distros():
            base = Path(rf"\\wsl.localhost\{distro}\home")
            try:
                users = list(base.iterdir())
            except OSError:
                continue
            for u in users:
                p = u / ".claude" / "projects"
                try:
                    if p.is_dir():
                        lb = f"WSL·{distro}"
                        out.append({"label": lb, "root": p, "key": _key(f"{lb}-{u.name}")})
                except OSError:
                    pass
    # 직접 추가: AIWM_EXTRA_SOURCES="이름=경로;이름=경로"
    for item in filter(None, os.environ.get("AIWM_EXTRA_SOURCES", "").split(";")):
        if "=" in item:
            lb, path = item.split("=", 1)
            if Path(path).is_dir():
                out.append({"label": lb.strip(), "root": Path(path), "key": _key(lb.strip())})
    return out


# ── 비밀값 가리기: 요약을 공유 폴더에 올리기 전에 적용 ──────────
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
_ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)(\s*[:=]\s*)([^\s\"\\,;]{4,})")


def redact(text):
    for rx in _SECRETS:
        text = rx.sub("[가림]", text)
    return _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + "[가림]", text)
