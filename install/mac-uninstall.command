#!/bin/bash
# 더블클릭: 자동 실행을 해제합니다. (모아 둔 요약은 ~/.ai-work-map 에 남습니다)
LABEL="kr.aiworkmap.agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/Desktop/AI 작업 지도.webloc"
echo "✅ 자동 실행을 해제했습니다."
