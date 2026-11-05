#!/usr/bin/env python3
"""새 버전 내보내기: VERSION을 올리고 커밋·태그·푸시한 뒤 GitHub 릴리스를 만든다.
릴리스가 생기면 설치된 모든 PC의 화면에 '새 버전' 알림이 뜬다.

  python3 tools/release.py 0.2.0 "바뀐 점 1" "바뀐 점 2"
GitHub 인증은 git에 저장된 로그인 정보(git credential)를 쓴다.
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from updater import REPO, _vt  # noqa: E402


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], check=True, capture_output=True, text=True).stdout.strip()


def token():
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, check=True)
    return next(l.split("=", 1)[1] for l in r.stdout.splitlines() if l.startswith("password="))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ver = sys.argv[1].lstrip("vV")
    notes = "\n".join(f"- {n}" for n in sys.argv[2:]) or "- 개선과 버그 수정"
    cur = (ROOT / "VERSION").read_text().strip()
    if _vt(ver) <= _vt(cur):
        sys.exit(f"새 버전({ver})이 지금 버전({cur})보다 커야 합니다.")
    if git("status", "--porcelain"):
        sys.exit("커밋하지 않은 변경이 있습니다. 먼저 커밋하세요.")
    (ROOT / "VERSION").write_text(ver + "\n")
    git("commit", "-am", f"v{ver}")
    git("tag", f"v{ver}")
    git("push", "origin", "HEAD", f"v{ver}")
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases", method="POST",
                                 data=json.dumps({"tag_name": f"v{ver}", "name": f"v{ver}", "body": notes}).encode(),
                                 headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        print("릴리스:", json.loads(r.read())["html_url"])


if __name__ == "__main__":
    main()
