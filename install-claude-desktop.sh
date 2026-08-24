#!/bin/bash
# 把雪球 MCP 装进 Claude 桌面应用（聊天界面用的 claude_desktop_config.json）。
#
# 背景：Claude 在运行时会用内存里的配置覆盖该文件，所以必须等它完全退出后再写。
# 本脚本会自动等待 Claude 退出 —— 先运行它，再去 Cmd+Q 退出 Claude 即可。
#
# 用法：在项目目录下先建好虚拟环境，再执行本脚本
#   uv venv --python 3.12 && uv pip install -e .
#   ./install-claude-desktop.sh

set -u
CONF="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
PROC="Claude.app/Contents/MacOS/Claude"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/.venv/bin/xueqiu-mcp"

echo "======================================================"
echo " 安装雪球 MCP 到 Claude 桌面应用"
echo "======================================================"
echo

if [ ! -x "$BIN" ]; then
  echo "✗ 没找到可执行文件：$BIN"
  echo "  请先在项目目录下执行：uv venv --python 3.12 && uv pip install -e ."
  exit 1
fi

# macOS 的 TCC 隐私保护会让 Claude.app 无法执行 Downloads / Desktop / Documents
# 里的文件（报 Operation not permitted），这类路径要先把项目挪到 ~/.local 下。
case "$BIN" in
  "$HOME"/Downloads/*|"$HOME"/Desktop/*|"$HOME"/Documents/*)
    echo "⚠️  项目位于 $(dirname "$HERE")，macOS 的隐私保护会阻止 Claude 执行这里的文件。"
    echo "   建议先把项目移到 ~/.local/share/xueqiu-mcp 再重新安装，例如："
    echo "     mkdir -p ~/.local/share && cp -R \"$HERE\" ~/.local/share/xueqiu-mcp"
    echo "     cd ~/.local/share/xueqiu-mcp && uv venv --python 3.12 && uv pip install -e . && ./install-claude-desktop.sh"
    echo
    printf "仍要继续？[y/N] "
    read -r ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || exit 1
    echo
    ;;
esac

if pgrep -f "$PROC" >/dev/null 2>&1; then
  echo "检测到 Claude 正在运行。"
  echo "→ 现在请切到 Claude，按 Cmd+Q 完全退出（不是关窗口）。"
  echo
  printf "等待 Claude 退出"
  while pgrep -f "$PROC" >/dev/null 2>&1; do
    printf "."
    sleep 2
  done
  echo
  echo "✓ Claude 已退出。"
  sleep 3   # 留出时间让它把最后一次写盘做完
else
  echo "✓ Claude 当前未运行。"
fi

echo
if [ -f "$CONF" ]; then
  BAK="$CONF.bak-$(date +%Y%m%d%H%M%S)"
  cp "$CONF" "$BAK"
  echo "已备份原配置：$(basename "$BAK")"
fi

XUEQIU_BIN="$BIN" /usr/bin/python3 - <<'PY'
import json, os, pathlib, sys

f = pathlib.Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
try:
    d = json.loads(f.read_text()) if f.exists() else {}
except Exception as e:
    print(f"✗ 现有配置无法解析：{e}")
    sys.exit(1)

# 只增改 xueqiu 一项，其余 MCP 原样保留
d.setdefault("mcpServers", {})["xueqiu"] = {"command": os.environ["XUEQIU_BIN"]}

f.parent.mkdir(parents=True, exist_ok=True)
f.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print("✓ 已写入配置：xueqiu")
PY

RC=$?
echo
if [ $RC -ne 0 ]; then
  echo "✗ 写入失败，原配置已备份，未做破坏性改动。"
  exit 1
fi

echo "正在重新打开 Claude..."
open -a Claude
echo
echo "完成。Claude 启动后，在聊天界面就能用雪球 MCP 了。"
