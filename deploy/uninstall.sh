#!/bin/bash
# 贾维斯系统卸载脚本：停止全部 LaunchAgent 并移除 plist（主服务 + Phase 2 语音两个）。
# 只动 ~/Library/LaunchAgents/ 下的 plist，项目代码/数据/日志全部保留。
set -euo pipefail

# 卸载顺序：先停依赖方（语音守护），再停 TTS worker，最后停主服务
for LABEL in com.yunxin.jarvis.voice com.yunxin.jarvis.tts com.yunxin.jarvis; do
    PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ -f "$PLIST_DST" ]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "==> 已停止服务并删除 $PLIST_DST"
    else
        echo "==> 未发现 $LABEL（$PLIST_DST 不存在），跳过"
    fi
done

echo "==> 卸载完成。项目目录、数据库 (data/)、日志 (logs/) 均已保留。"
