#!/bin/bash
# =====================================================================
# Brevet + Claude (Desktop / Cowork): one-time local setup
#
#   1. finds Python 3.10+ and creates an isolated env at ~/.brevet/venv
#   2. installs Brevet with the MCP extra (from this clone if run inside
#      the repo, otherwise from GitHub)
#   3. creates your governed workspace (default: ~/brevet-cowork)
#   4. sets YOUR identity and mission group in the signed manifest
#   5. registers the Brevet MCP server with the Claude desktop app
#
# Usage:
#   bash setup.sh [--workspace DIR] [--owner you@org.com]
#                 [--mission-group name] [--claude-config PATH]
#                 [--chap-workspace wsp_id]
#
# Defaults: workspace ~/brevet-cowork; owner from `git config user.email`;
# mission group "review_board"; Claude config at the macOS location.
# Safe to re-run. Nothing leaves your machine.
# =====================================================================
set -euo pipefail

WORKSPACE="$HOME/brevet-cowork"
OWNER_EMAIL="$(git config user.email 2>/dev/null || true)"
MISSION="review_board"
CLAUDE_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
CHAP_WS=""   # set with --chap-workspace to enable the CHAP relay

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)      WORKSPACE="$2"; shift 2;;
    --owner)          OWNER_EMAIL="$2"; shift 2;;
    --mission-group)  MISSION="$2"; shift 2;;
    --claude-config)  CLAUDE_CFG="$2"; shift 2;;
    --chap-workspace) CHAP_WS="$2"; shift 2;;
    *) echo "unknown option: $1"; exit 1;;
  esac
done
if [ -z "$OWNER_EMAIL" ]; then
  printf "Your email (used as the accountable identity human:<email>): "
  read -r OWNER_EMAIL
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.brevet/venv"
BREVET="$VENV_DIR/bin/brevet"

echo "==> [1/5] Checking Python (need 3.10+)"
PY=""
for CAND in python3.13 python3.12 python3.11 python3.10 python3 \
            /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
            /usr/local/bin/python3.12; do
  if command -v "$CAND" >/dev/null 2>&1 && \
     "$CAND" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$(command -v "$CAND")"; break
  fi
done
[ -n "$PY" ] || { echo "No Python 3.10+ found. brew install python@3.12"; exit 1; }
echo "    using $PY ($("$PY" -V))"

echo "==> [2/5] Installing Brevet into $VENV_DIR"
mkdir -p "$HOME/.brevet"
"$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
if [ -f "$SCRIPT_DIR/../../pyproject.toml" ]; then
  "$VENV_DIR/bin/pip" install --quiet -e "$(cd "$SCRIPT_DIR/../.." && pwd)[mcp]"
else
  "$VENV_DIR/bin/pip" install --quiet "brevet[mcp] @ git+https://github.com/BrightbeamAI/brevet"
fi
"$VENV_DIR/bin/pip" install --quiet "mcp>=1.2"
"$VENV_DIR/bin/python" -c "import brevet, mcp" || {
  echo "    ERROR: brevet or mcp failed to import"; exit 1; }
echo "    installed: brevet + mcp OK"

echo "==> [3/5] Creating your governed workspace: $WORKSPACE"
mkdir -p "$WORKSPACE"
cp -R "$SCRIPT_DIR/tools" "$WORKSPACE/" 2>/dev/null || true
mkdir -p "$WORKSPACE/governed"
[ -f "$WORKSPACE/governed/families.yaml" ] || \
  cp "$SCRIPT_DIR/governed/families.yaml" "$WORKSPACE/governed/families.yaml"
# pre-approve the brevet tools for sessions in this folder: governance
# that interrupts the work gets switched off
mkdir -p "$WORKSPACE/.claude"
[ -f "$WORKSPACE/.claude/settings.json" ] || \
  cp "$SCRIPT_DIR/settings/claude-settings.json" "$WORKSPACE/.claude/settings.json"
if [ -n "$CHAP_WS" ]; then
  mkdir -p "$WORKSPACE/chap-sink"
  [ -f "$WORKSPACE/chap-sink/README.md" ] || \
    cp "$SCRIPT_DIR/chap-sink-README.md" "$WORKSPACE/chap-sink/README.md"
fi
if [ ! -f "$WORKSPACE/agent.yaml" ]; then
  ( cd "$WORKSPACE" && "$BREVET" init )
else
  echo "    workspace already initialised, leaving the chain untouched"
fi

echo "==> [4/5] Setting your identity in the signed manifest"
"$VENV_DIR/bin/python" - "$WORKSPACE/agent.yaml" "$OWNER_EMAIL" "$MISSION" <<'PYEOF'
import sys, yaml
p, email, mission = sys.argv[1], sys.argv[2], sys.argv[3]
m = yaml.safe_load(open(p))
if m.get("agent") in (None, "my_agent"):
    m["agent"] = "cowork_assistant"   # readable name in status and lockfiles
ident = m.setdefault("identity_policy", {})
if ident.get("agent_id") in (None, "my_agent"):
    ident["agent_id"] = "cowork_assistant"
ident["owner"] = f"human:{email}"
ident["mission_group"] = f"mission_group:{mission}"
open(p, "w").write(yaml.safe_dump(m, sort_keys=False))
print(f"    agent: {m['agent']}   owner: human:{email}   mission group: mission_group:{mission}")
PYEOF

echo "==> [5/5] Registering the Brevet MCP server with Claude"
"$VENV_DIR/bin/python" - "$CLAUDE_CFG" "$BREVET" "$WORKSPACE" <<'PYEOF'
import json, os, sys, time
cfg_path, brevet_bin, ws = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = {}
if os.path.exists(cfg_path):
    open(cfg_path + ".bak-" + time.strftime("%Y%m%d%H%M%S"), "w").write(open(cfg_path).read())
    try:
        cfg = json.load(open(cfg_path))
    except ValueError:
        cfg = {}
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
cfg.setdefault("mcpServers", {})["brevet"] = {
    "command": brevet_bin,
    "args": ["mcp", "--workdir", os.path.join(ws, ".brevet"),
             "--manifest-path", os.path.join(ws, "agent.yaml")]}
json.dump(cfg, open(cfg_path, "w"), indent=2)
print(f"    wrote {cfg_path} (backup saved beside it)")
PYEOF

echo ""
echo "======================================================================"
echo " Done. Final steps:"
echo "  1. Quit Claude completely and reopen it."
echo "  2. Save the capture skill: open skill/SKILL.md from this example in"
echo "     a Claude chat and say 'save this as a skill named brevet-capture'."
echo "  3. In a new conversation, try: 'call brevet_status'."
echo " Your workspace: $WORKSPACE   (ledger, keys, lockfile under .brevet/)"
echo "   4. Schedule the weekly dawn digest: paste digest/dawn-digest.md"
echo "      into Claude and ask for a Friday 9am scheduled task, then"
echo "      click Run now once to pre-approve its tools."
if [ -n "$CHAP_WS" ]; then
echo "   CHAP relay enabled for workspace $CHAP_WS: the digest reads the"
echo "      audit into $WORKSPACE/chap-sink and ingests it as evidence."
fi
echo " Tool permissions: $WORKSPACE/.claude/settings.json pre-approves the"
echo "   brevet tools for sessions in that folder. Copy it into any other"
echo "   project folder you use, or choose Always allow at the first prompt."
echo "======================================================================"
