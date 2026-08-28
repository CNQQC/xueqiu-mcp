#!/usr/bin/env bash
# 把 .src 里检出的代码同步到运行目录并重启服务。由 /usr/local/bin/xueqiu-deploy 调起，
# 调用方保证 .src 已经更新到 origin/main。
#
# 只同步包目录 xueqiu_mcp/。serve_http.py 和 .env 是服务器独有的，不在仓库里，
# 不能被 --delete 波及 —— 所以同步的是包目录本身，而不是整个应用目录。
#
# 失败路径比成功路径重要：坏代码进来时，服务必须还能起着。
set -uo pipefail

APP_DIR=/opt/xueqiu-mcp
SRC_DIR="$APP_DIR/.src"
PKG="$APP_DIR/xueqiu_mcp"
MARK="$APP_DIR/.deployed-sha"
SERVICE=xueqiu-mcp.service
PORT="${XUEQIU_PORT:-18061}"
KEEP_BACKUPS=3

log() { echo "[deploy] $*"; }

# MCP 握手成功才算健康。只看 systemctl is-active 不够：进程起着但 import 失败或
# 端口没监听的情况，systemd 照样报 active（Restart=always 会让它反复重启）。
health() {
  local code
  for _ in $(seq 1 20); do
    code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
      -X POST "http://127.0.0.1:$PORT/mcp" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deploy-healthcheck","version":"1"}}}' \
      2>/dev/null)
    [ "$code" = "200" ] && return 0
    sleep 1
  done
  return 1
}

target=$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null) || { log "读不到 .src 的 HEAD"; exit 1; }
current=$(cat "$MARK" 2>/dev/null || echo "")
if [ "$target" = "$current" ]; then
  exit 0                      # 无变化。定时器每几分钟跑一次，这里必须安静退出
fi
short_cur=${current:0:8}; [ -z "$short_cur" ] && short_cur="无"
log "发现新版本 ${target:0:8}（当前 $short_cur）"

backup="$APP_DIR/xueqiu_mcp.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$PKG" "$backup" || { log "备份失败，中止"; exit 1; }

if ! rsync -a --delete --exclude '__pycache__' "$SRC_DIR/src/xueqiu_mcp/" "$PKG/"; then
  log "同步失败，还原并中止"
  rm -rf "$PKG"; mv "$backup" "$PKG"
  exit 1
fi

# 重启之前先在旁边把代码 import 一遍。语法错和缺依赖能在这里就拦住，
# 服务连停都不用停 —— 坏提交的代价降到零停机。
if ! (cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -c "import xueqiu_mcp.server" 2>&1); then
  log "新代码导入失败，回滚且不重启服务（服务未受影响）"
  rm -rf "$PKG"; mv "$backup" "$PKG"
  exit 1
fi

systemctl restart "$SERVICE"
if health; then
  echo "$target" > "$MARK"
  log "部署成功 ${target:0:8}"
  # 只留最近几份备份，1.6G 的机器上磁盘和内存一样要省
  ls -1dt "$APP_DIR"/xueqiu_mcp.bak-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -rf
  exit 0
fi

log "健康检查未通过，回滚到上一版"
rm -rf "$PKG"; mv "$backup" "$PKG"
systemctl restart "$SERVICE"
if health; then
  log "已回滚，服务恢复正常。${target:0:8} 未部署"
else
  log "回滚后仍不健康 —— 需要人工介入"
fi
exit 1
