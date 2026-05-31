#!/usr/bin/env bash
# browser-eyes installer
set -euo pipefail

INSTALL_DIR="${HOME}/.local/share/browser-eyes"
BIN_DIR="${HOME}/.local/bin"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}" "${BIN_DIR}"
rm -rf "${INSTALL_DIR}/src"
cp -r "${SRC_DIR}" "${INSTALL_DIR}/src"
chmod +x "${INSTALL_DIR}/src/"*.py

echo "==> Checking system deps"
need=()
command -v python3 >/dev/null || need+=("python3")

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
  command -v grim >/dev/null || command -v gnome-screenshot >/dev/null \
    || command -v spectacle >/dev/null \
    || need+=("grim or gnome-screenshot or spectacle (wayland screenshot)")
else
  command -v scrot >/dev/null || command -v maim >/dev/null \
    || command -v import >/dev/null \
    || need+=("scrot or maim or imagemagick (x11 screenshot)")
fi

have_browser=0
for b in google-chrome google-chrome-stable chromium chromium-browser \
         brave-browser microsoft-edge vivaldi; do
  command -v "$b" >/dev/null && have_browser=1 && break
done
[ $have_browser -eq 0 ] && need+=("a chromium-family browser")

if [ ${#need[@]} -gt 0 ]; then
  echo "!! Missing system dependencies:"
  printf " - %s\n" "${need[@]}"
  echo "   (Daemon will report what's missing at runtime if you continue.)"
  echo ""
fi

echo "==> Creating Python venv"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet aiohttp websockets "mcp>=1.0.0"

echo "==> Registering MCP server with Claude Code"
if command -v claude >/dev/null; then
  claude mcp remove browser-eyes 2>/dev/null || true
  claude mcp add browser-eyes -- "${INSTALL_DIR}/venv/bin/python3" "${INSTALL_DIR}/src/mcp_server.py" \
    || echo "   (auto-register failed -- add it manually)"
else
  echo "   'claude' command not found -- register manually:"
  echo "   claude mcp add browser-eyes -- ${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/src/mcp_server.py"
fi

echo ""
echo "================================================================"
echo "  Installed."
echo ""
echo "  Quick start:"
echo "    python3 daemon.py   # launch daemon + browser"
echo "    In Claude Code, ask: what's on my screen?"
echo "================================================================"
