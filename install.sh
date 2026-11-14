#!/usr/bin/env bash
# Install 云村邮件通 into an ncmm home using the subdirectory layout
# ({home}/plugins/ncmm-mail/), the same shape used by ncmm-qqbot.
# Never overwrites an existing config.yaml, never touches notify.yaml.
set -euo pipefail

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${NCMM_HOME_DIR:-/opt/ncmm/home}"
PLUGIN_DIR="${HOME_DIR}/plugins/ncmm-mail"
BIN=/opt/ncmm/bin/ncmm

echo "[1/6] preflight"
[ -x "$BIN" ] || { echo "ncmm binary not found: $BIN"; exit 1; }
command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
mkdir -p "$PLUGIN_DIR"

echo "[2/6] remove legacy single-file install (if any)"
if [ -f "${HOME_DIR}/plugins/ncmm-mail.py" ]; then
  rm -f "${HOME_DIR}/plugins/ncmm-mail.py"
  echo "  removed old plugins/ncmm-mail.py to avoid duplicate discovery"
fi

echo "[3/6] install plugin files"
install -m 0755 "$PKG/ncmm-mail.py" "$PLUGIN_DIR/ncmm-mail.py"
# ncmm discovers the no-extension entry first (same layout as ncmm-qqbot),
# so provide that as the primary executable.
cat > "$PLUGIN_DIR/ncmm-mail" <<'WRAP'
#!/usr/bin/env python3
import os, runpy, sys
here = os.path.dirname(os.path.abspath(__file__))
sys.argv[0] = os.path.join(here, "ncmm-mail.py")
runpy.run_path(os.path.join(here, "ncmm-mail.py"), run_name="__main__")
WRAP
chmod 0755 "$PLUGIN_DIR/ncmm-mail"
install -m 0644 "$PKG/plugin.json" "$PLUGIN_DIR/plugin.json"
install -m 0644 "$PKG/source.json" "$PLUGIN_DIR/source.json"
install -m 0644 "$PKG/config.yaml.example" "$PLUGIN_DIR/config.yaml.example"
if [ -f "$PLUGIN_DIR/config.yaml" ]; then
  echo "  keeping existing config.yaml"
else
  install -m 0600 "$PKG/config.yaml.example" "$PLUGIN_DIR/config.yaml"
  echo "  created config.yaml (dry_run=true, fill in credentials)"
fi
chown -R ncmm:ncmm "$PLUGIN_DIR" 2>/dev/null || true

echo "[4/6] bridge unit + env template (installed, NOT enabled)"
install -m 0644 "$PKG/ncmm-mail-bridge.service" /etc/systemd/system/ncmm-mail-bridge.service
if [ ! -f /etc/ncmm-mail.env ]; then
  install -m 0640 "$PKG/ncmm-mail.env.example" /etc/ncmm-mail.env
fi
systemctl daemon-reload

echo "[5/6] verify discovery"
"$BIN" --home "$HOME_DIR" plugin list 2>&1 | sed -n '1,20p'

echo "[6/6] verify contract"
echo "--- config check (incomplete is EXPECTED before credentials are filled) ---"
python3 "$PLUGIN_DIR/ncmm-mail.py" --home "$HOME_DIR" --check < /dev/null 2>&1 | sed -n '1,4p' || true
echo "--- dry-run email ---"
"$BIN" --home "$HOME_DIR" plugin run mail --test --title "云村邮件通自检" 2>&1 | sed -n '1,25p'

echo
echo "Next:"
echo "  WebUI 插件中心 → 「云村邮件通」→【在线配置】填发件人/发件邮箱/收件人"
echo "  或启动面板: systemctl enable --now ncmm-mail-bridge → http://127.0.0.1:8787/"
echo "  失败通知转发: 见 README.md 的 notify.yaml 片段"
