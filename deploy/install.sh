#!/bin/bash
# 木木系统安装脚本（macOS）：渲染 LaunchAgent → 加载常驻服务 → 健康检查。
# Phase 2：VOICE_ENABLED=1 时额外装载 TTS worker（:TTS_PORT）与语音守护，共三个服务。
# 幂等可重跑：重复执行 = 重新渲染 + 重载服务。
set -euo pipefail

# pwd -P 解析软链：从 ~/Desktop/开发/贾维斯系统 软链执行也渲染真实路径进 plist
# （launchd 进程经 ~/Desktop 路径读文件会被 TCC 卡死，见 CLAUDE.md 血泪教训）
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
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
# 权限收紧（审查修复）：db/日志含 prompt、任务结果、审批明细，仅本用户可读。
# 预建日志文件并 chmod——launchd 对已存在文件追加写、不会重置权限；
# jarvis.db 的 -wal/-shm 由 SQLite 继承主库权限（db.py 内亦有兜底）。
for f in jarvis tts voice; do
    touch "$ROOT/logs/$f.out.log" "$ROOT/logs/$f.err.log"
done
chmod 600 "$ROOT"/logs/*.log 2>/dev/null || true
chmod 600 "$ROOT"/data/jarvis.db* "$ROOT"/data/*.json 2>/dev/null || true

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

# ---------- 7. 语音服务（Phase 2，VOICE_ENABLED=1 时启用） ----------
# 语义对齐 jarvis/config.py：缺省=1（启用）；显式 0/false/空 = 关闭
if grep -qE '^VOICE_ENABLED=' "$ROOT/.env"; then
    VOICE_ENABLED="$(env_get VOICE_ENABLED)"
else
    VOICE_ENABLED=1
fi
TTS_PLIST_SRC="$ROOT/deploy/com.yunxin.jarvis.tts.plist"
TTS_PLIST_DST="$HOME/Library/LaunchAgents/com.yunxin.jarvis.tts.plist"
VOICE_PLIST_SRC="$ROOT/deploy/com.yunxin.jarvis.voice.plist"
VOICE_PLIST_DST="$HOME/Library/LaunchAgents/com.yunxin.jarvis.voice.plist"
VOICE_ON=0
TTS_PORT=""

case "$VOICE_ENABLED" in
    0|false|"")
        # 关闭语音：跳过安装；之前装过的顺手卸掉，保持与 .env 一致（幂等）
        echo "==> VOICE_ENABLED=$VOICE_ENABLED，跳过语音服务"
        for P in "$VOICE_PLIST_DST" "$TTS_PLIST_DST"; do
            if [ -f "$P" ]; then
                launchctl unload "$P" 2>/dev/null || true
                rm -f "$P"
                echo "==> 已卸载残留语音服务: $P"
            fi
        done
        ;;
    *)
        INDEX_TTS_DIR="$(env_get INDEX_TTS_DIR)"; INDEX_TTS_DIR="${INDEX_TTS_DIR:-/Users/yunxin/Desktop/开发/index-tts}"
        TTS_PORT="$(env_get TTS_PORT)"; TTS_PORT="${TTS_PORT:-8778}"
        VOICE_REF="$(env_get VOICE_REF)"; VOICE_REF="${VOICE_REF:-$ROOT/workspace/voice/jarvis_ref.wav}"
        TTS_PY="$INDEX_TTS_DIR/.venv/bin/python"
        VOICE_PY="$ROOT/.venv-voice/bin/python"

        # 7.1 前置检查
        if [ ! -x "$TTS_PY" ]; then
            echo "错误: 未找到 index-tts venv 解释器 ($TTS_PY)" >&2
            echo "请确认 .env 的 INDEX_TTS_DIR 指向已装好依赖的 index-tts 仓库" >&2
            exit 1
        fi
        if [ ! -x "$VOICE_PY" ]; then
            echo "错误: 未找到语音 venv 解释器 ($VOICE_PY)" >&2
            echo "请先执行:" >&2
            echo "  /opt/homebrew/bin/python3.12 -m venv \"$ROOT/.venv-voice\"" >&2
            echo "  \"$ROOT/.venv-voice/bin/pip\" install -r \"$ROOT/reqs-voice.txt\"" >&2
            exit 1
        fi
        if [ ! -f "$VOICE_REF" ]; then
            echo "错误: 缺少参考音色文件 ($VOICE_REF)" >&2
            exit 1
        fi
        # 唤醒词 KWS 模型（models/ 不进 git，缺失时 voice 服务会 KeepAlive 崩溃循环）
        KWS_DIR="$ROOT/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        for f in tokens.txt \
                 encoder-epoch-12-avg-2-chunk-16-left-64.onnx \
                 decoder-epoch-12-avg-2-chunk-16-left-64.onnx \
                 joiner-epoch-12-avg-2-chunk-16-left-64.onnx; do
            if [ ! -f "$KWS_DIR/$f" ]; then
                echo "错误: 缺少唤醒词 KWS 模型文件 ($KWS_DIR/$f)" >&2
                echo "下载（走代理）:" >&2
                echo "  curl -sL -x http://192.168.50.4:7890 -o /tmp/kws.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2" >&2
                echo "  mkdir -p \"$ROOT/models\" && tar -xjf /tmp/kws.tar.bz2 -C \"$ROOT/models/\"" >&2
                exit 1
            fi
        done
        mkdir -p "$ROOT/data/voice_cache"

        # 7.2 渲染并安装两个 plist
        sed -e "s|__JARVIS_ROOT__|$ROOT|g" \
            -e "s|__INDEX_TTS_DIR__|$INDEX_TTS_DIR|g" \
            -e "s|__VOICE_REF__|$VOICE_REF|g" \
            -e "s|__TTS_PORT__|$TTS_PORT|g" \
            "$TTS_PLIST_SRC" > "$TTS_PLIST_DST"
        plutil -lint "$TTS_PLIST_DST" >/dev/null
        sed -e "s|__JARVIS_ROOT__|$ROOT|g" \
            "$VOICE_PLIST_SRC" > "$VOICE_PLIST_DST"
        plutil -lint "$VOICE_PLIST_DST" >/dev/null
        echo "==> 已安装语音 LaunchAgents: com.yunxin.jarvis.tts / com.yunxin.jarvis.voice"

        # 7.3 重载两个服务（幂等）
        launchctl unload "$TTS_PLIST_DST" 2>/dev/null || true
        launchctl load "$TTS_PLIST_DST"
        launchctl unload "$VOICE_PLIST_DST" 2>/dev/null || true
        launchctl load "$VOICE_PLIST_DST"

        # 7.4 TTS 健康检查：模型加载约 20s + 暖机合成，慢——重试 60 次，每次间隔 2s
        echo "==> 等待 TTS worker 加载模型（约 30s，最长等 2 分钟）..."
        TTS_OK=0
        for _ in $(seq 1 60); do
            if curl -sf -m 2 "http://127.0.0.1:$TTS_PORT/healthz" >/dev/null 2>&1; then
                TTS_OK=1
                break
            fi
            sleep 2
        done
        if [ "$TTS_OK" != "1" ]; then
            echo "错误: TTS /healthz 连续 60 次未通过，请查日志:" >&2
            echo "  tail -50 \"$ROOT/logs/tts.err.log\"" >&2
            exit 1
        fi
        echo "==> TTS worker 在线: http://127.0.0.1:$TTS_PORT/healthz"

        # 7.5 voice daemon 无健康端点：确认 launchd 已接管
        # 不用 grep -q：避免 pipefail 下 launchctl 收 SIGPIPE 造成误报
        if launchctl list 2>/dev/null | grep "com.yunxin.jarvis.voice" >/dev/null; then
            echo "==> 语音守护进程已装载（日志: $ROOT/logs/voice.{out,err}.log）"
        else
            echo "警告: launchctl list 未见 com.yunxin.jarvis.voice，请查 logs/voice.err.log" >&2
        fi
        echo "==> 提示: 首次需在弹窗允许麦克风（系统设置 → 隐私与安全性 → 麦克风）"
        VOICE_ON=1
        ;;
esac

# ---------- 8. 打印控制台地址（token 只显示掩码） ----------
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
TOKEN_MASKED="${TOKEN:0:4}****${TOKEN: -4}"
echo ""
echo "================== 木木系统在线 =================="
echo "  本机控制台 : http://localhost:$PORT"
if [ -n "$LAN_IP" ]; then
    echo "  局域网访问 : http://$LAN_IP:$PORT  (iPhone 同一 WiFi 可加主屏)"
fi
echo "  访问令牌   : $TOKEN_MASKED  (完整值见 $ROOT/.env 的 JARVIS_TOKEN)"
if [ "$VOICE_ON" = "1" ]; then
    echo "  语音服务   : TTS 127.0.0.1:$TTS_PORT + 唤醒词（喊一声 \"木木\" 试试）"
fi
echo "  卸载方式   : bash \"$ROOT/deploy/uninstall.sh\""
echo "====================================================="
