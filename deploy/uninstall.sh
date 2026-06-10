#!/bin/bash
# 贾维斯系统卸载脚本：停止 LaunchAgent 并移除 plist。
# 只动 ~/Library/LaunchAgents/ 下的 plist，项目代码/数据/日志全部保留。
set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.yunxin.jarvis.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "==> 已停止服务并删除 $PLIST_DST"
else
    echo "==> 未发现已安装的 LaunchAgent（$PLIST_DST 不存在），无需卸载"
fi

echo "==> 卸载完成。项目目录、数据库 (data/)、日志 (logs/) 均已保留。"
