#!/bin/bash
# 더블클릭: AI 작업 지도를 로그인할 때마다 백그라운드에서 자동 실행되게 등록합니다.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(python3 -c 'import sys; print(sys.executable)')"
LABEL="kr.aiworkmap.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.ai-work-map"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/aiworkmap.py</string><string>run</string><string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME/.ai-work-map/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.ai-work-map/agent.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# 바로가기: 홈 폴더에 웹 링크
cat > "$HOME/Desktop/AI 작업 지도.webloc" <<WL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>http://127.0.0.1:8765/</string></dict></plist>
WL
sleep 2
echo "✅ 설치했습니다. 로그인할 때마다 자동으로 실행됩니다."
echo "   화면: http://127.0.0.1:8765/  (바탕화면 'AI 작업 지도' 바로가기)"
open "http://127.0.0.1:8765/"
