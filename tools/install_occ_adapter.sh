#!/bin/sh
# Optional, user-invoked adapter build. OpenChatCut remains a separate dependency.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$ROOT/runtime/openchatcut"
[ ! -e "$DEST" ] || { printf '%s\n' '目标目录已存在；不会覆盖，请先自行核对。' >&2; exit 1; }
case "$(node -p 'process.versions.node.split(".")[0]')" in 24) ;; *) printf '%s\n' '需要 Node.js 24.x。' >&2; exit 1;; esac
mkdir -p "$ROOT/runtime"
git clone --depth 1 --branch v0.2.16 https://github.com/0xsline/OpenChatCut.git "$DEST"
python3 - "$DEST" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])/'server/plugins/extension-store.ts'
s=p.read_text();old="const DEFAULT_ROOT_DIR = join(homedir(), '.openchatcut', 'plugins');"
if s.count(old)!=1:raise SystemExit('不兼容的源码版本，已停止。')
p.write_text(s.replace(old,"const DEFAULT_ROOT_DIR = process.env.MIXCUT_CLI_PLUGIN_DIR || join(homedir(), '.openchatcut', 'plugins');"))
PY
cd "$DEST"
npm ci
npm run build:cli
printf '%s\n' 'CLI 适配器已构建。OpenChatCut 桌面应用需要另行安装。'
