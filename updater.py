"""AI 작업 지도 — 새 버전 확인과 업데이트.

GitHub 릴리스(JinyoungPark/MyWorkingMap)의 최신 버전과 VERSION 파일을 비교한다.
업데이트는 git으로 받은 폴더면 git pull, 아니면 릴리스 zip을 받아 파일을 덮어쓴 뒤 다시 시작한다.
"""
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

REPO = "JinyoungPark/MyWorkingMap"
HERE = Path(__file__).resolve().parent
CHECK_EVERY = 6 * 3600
_UA = {"User-Agent": "MyWorkingMap-updater", "Accept": "application/vnd.github+json"}


def current_version():
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _vt(v):
    parts = []
    for x in str(v).lstrip("vV").split("."):
        num = "".join(ch for ch in x if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Updater:
    def __init__(self, log=print, notify=True):
        self.log = log
        self.notify = notify
        self.lock = threading.Lock()
        self.state = {"current": current_version(), "latest": None, "available": False, "notes": "",
                      "url": f"https://github.com/{REPO}/releases", "checked": None, "error": None,
                      "mode": "git" if (HERE / ".git").exists() else "zip", "busy": False}
        self._notified = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def check(self):
        try:
            rel = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
            tag = rel.get("tag_name") or ""
            with self.lock:
                self.state.update(latest=tag.lstrip("vV"), notes=(rel.get("body") or "")[:4000],
                                  url=rel.get("html_url") or self.state["url"], tag=tag,
                                  available=_vt(tag) > _vt(self.state["current"]), error=None)
        except urllib.error.HTTPError as e:
            # 404 = 아직 릴리스가 없음
            with self.lock:
                self.state.update(error=None if e.code == 404 else f"GitHub 응답 {e.code}", available=False)
        except Exception as e:
            with self.lock:
                self.state.update(error=f"확인 실패: {e.__class__.__name__}")
        with self.lock:
            self.state["checked"] = datetime.now().isoformat(timespec="seconds")
            st = dict(self.state)
        if st["available"] and self.notify and self._notified != st["latest"]:
            self._notified = st["latest"]
            self.log(f"새 버전 {st['latest']} 있음 (현재 {st['current']})")
            _os_notify("AI 작업 지도", f"새 버전 v{st['latest']}이 있습니다. 화면에서 업데이트할 수 있어요.")
        return st

    def loop(self):
        time.sleep(10)
        while True:
            self.check()
            time.sleep(CHECK_EVERY)

    # ── 적용 ──
    def apply(self):
        st = self.check()
        if not st["available"]:
            return False, "이미 최신 버전입니다."
        with self.lock:
            if self.state["busy"]:
                return False, "업데이트가 이미 진행 중입니다."
            self.state["busy"] = True
        try:
            if st["mode"] == "git":
                r = subprocess.run(["git", "-C", str(HERE), "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout).strip()[:300])
            else:
                self._apply_zip(st["tag"])
            self.log(f"업데이트 완료 → {st['latest']}, 다시 시작합니다")
            return True, f"v{st['latest']}로 업데이트했습니다. 잠시 후 다시 시작합니다."
        except Exception as e:
            with self.lock:
                self.state["busy"] = False
            self.log(f"업데이트 실패: {e}")
            return False, f"업데이트 실패: {e}"

    def _apply_zip(self, tag):
        data = _get(f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip", timeout=60)
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        top = names[0].split("/")[0] + "/"
        if not (top.rstrip("/") and all(n.startswith(top) for n in names)):
            raise RuntimeError("압축 파일 구조가 예상과 다릅니다")
        if top + "aiworkmap.py" not in names or top + "VERSION" not in names:
            raise RuntimeError("릴리스에 필요한 파일이 없습니다")
        for info in zf.infolist():
            rel = info.filename[len(top):]
            if not rel or info.is_dir():
                continue
            if rel.startswith(("/", ".git/")) or ".." in Path(rel).parts:
                continue
            dest = HERE / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(zf.read(info))
            mode = (info.external_attr >> 16) & 0o777
            if mode and platform.system() != "Windows":
                os.chmod(tmp, mode)
            os.replace(tmp, dest)


def restart():
    """같은 명령으로 다시 시작한다 (새 코드 적용)."""
    args = [sys.executable, str(HERE / "aiworkmap.py")] + [a for a in sys.argv[1:]]
    if "--no-open" not in args:
        args.append("--no-open")
    if platform.system() == "Windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(args, cwd=str(HERE), creationflags=flags, close_fds=True)
        os._exit(0)
    os.execv(sys.executable, args)


def _os_notify(title, msg):
    try:
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], timeout=5)
        elif platform.system() == "Windows":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                  "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                  f"$x=$t.GetElementsByTagName('text');$x.Item(0).AppendChild($t.CreateTextNode('{title}'))>$null;$x.Item(1).AppendChild($t.CreateTextNode('{msg}'))>$null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Windows PowerShell').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
