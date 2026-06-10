#!/bin/bash
# 贾维斯系统安装脚本（macOS）：渲染 LaunchAgent → 加载常驻服务 → 健康检查。
# 幂等可重跑：重复执行 = 重新渲染 + 重载服务。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
PLIST_SRC="$ROOT/deploy/com.yunxin.jarvis.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.yunxin.jarvis.plist"
LABEL="com.yunxin.jarvis"

echo "==> 项目根目录: $ROOT"

# ---------- 1. 检查 venv ----------
if [ ! -x "$VENV_PY" ]; then
    echo "错误: 未找到 venv 解释器 ($VENV_PY)" >&2
    echo "请先执行:" >&2
    echo "  /opt/homebrew/bin/python3.12 -m venv \"$ROOT/.venv\"" >&2
    echo "  \"$ROOT/.venv/bin/pip\" install -r \"$ROOT/requirements.txt\"" >&2
    exit 1
fi

# ---------- 2. 生成/校验 .env ----------
if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "==> 已从 .env.example 生成 .env"
fi

# 从 .env 取值（取最后一次出现的赋值，带默认值兜底）
env_get() { grep -E "^$1=" "$ROOT/.env" | tail -1 | cut -d= -f2- || true; }

# JARVIS_TOKEN 为空或仍是占位符 → 自动生成随机 token（幂等：已有真实 token 不动）
TOKEN="$(env_get JARVIS_TOKEN)"
case "$TOKEN" in
    ""|*openssl*|*改成*)
        TOKEN="$(openssl rand -hex 16)"
        if grep -qE '^JARVIS_TOKEN=' "$ROOT/.env"; then
            # BSD sed：-i 后必须跟空串
            sed -i '' "s|^JARVIS_TOKEN=.*|JARVIS_TOKEN=$TOKEN|" "$ROOT/.env"
        else
            printf 'JARVIS_TOKEN=%s\n' "$TOKEN" >> "$ROOT/.env"
        fi
        echo "==> 已生成随机 JARVIS_TOKEN"
        ;;
esac

HOST="$(env_get JARVIS_HOST)"; HOST="${HOST:-0.0.0.0}"
PORT="$(env_get JARVIS_PORT)"; PORT="${PORT:-8777}"

# ---------- 3. 目录 ----------
mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/workspace" "$HOME/Library/LaunchAgents"

# ---------- 4. 渲染并安装 plist ----------
sed -e "s|__JARVIS_ROOT__|$ROOT|g" \
    -e "s|__VENV_PY__|$VENV_PY|g" \
    -e "s|__JARVIS_HOST__|$HOST|g" \
    -e "s|__JARVIS_PORT__|$PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null
echo "==> 已安装 LaunchAgent: $PLIST_DST"

# ---------- 5. 重载服务（先卸载旧的，忽略报错 → 幂等） ----------
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "==> 服务已加载: $LABEL"

# ---------- 6. 健康检查（重试 10 次，每次间隔 1s） ----------
echo "==> 等待服务启动..."
HEALTH_OK=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done
if [ "$HEALTH_OK" != "1" ]; then
    echo "错误: /healthz 连续 10 次未通过，请查日志:" >&2
    echo "  tail -50 \"$ROOT/logs/jarvis.err.log\"" >&2
    exit 1
fi
echo "==> 健康检查通过: http://127.0.0.1:$PORT/healthz"

# ---------- 7. 打印控制台地址（token 只显示掩码） ----------
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
TOKEN_MASKED="${TOKEN:0:4}****${TOKEN: -4}"
echo ""
echo "================== 贾维斯系统在线 =================="
echo "  本机控制台 : http://localhost:$PORT"
if [ -n "$LAN_IP" ]; then
    echo "  局域网访问 : http://$LAN_IP:$PORT  (iPhone 同一 WiFi 可加主屏)"
fi
echo "  访问令牌   : $TOKEN_MASKED  (完整值见 $ROOT/.env 的 JARVIS_TOKEN)"
echo "  卸载方式   : bash \"$ROOT/deploy/uninstall.sh\""
echo "====================================================="
