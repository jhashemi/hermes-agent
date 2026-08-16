#!/bin/bash
# Install kanban-rule-distiller systemd unit and wrapper script

set -e

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_BIN="${HOME}/.hermes/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "Installing Kanban Rule Distiller..."

# Create ~/.hermes/bin if needed
mkdir -p "$HERMES_BIN"

# Create wrapper script
cat > "$HERMES_BIN/kanban-rule-distiller" <<'EOF'
#!/bin/bash
# Wrapper for kanban-rule-distiller daemon
cd "$(dirname "$(python3 -c "import hermes_agent; print(hermes_agent.__file__)")" | head -1)/.."
exec python3 -m "plugins.kanban_rule_distiller.src.daemon" "$@"
EOF

chmod +x "$HERMES_BIN/kanban-rule-distiller"
echo "✓ Created wrapper: $HERMES_BIN/kanban-rule-distiller"

# Install systemd unit
mkdir -p "$SYSTEMD_USER_DIR"
cp "$PLUGIN_DIR/systemd/kanban-rule-distiller.service" "$SYSTEMD_USER_DIR/"
echo "✓ Installed systemd unit: $SYSTEMD_USER_DIR/kanban-rule-distiller.service"

# Reload systemd
systemctl --user daemon-reload
echo "✓ Reloaded systemd user manager"

# Optional: enable and start
if [[ "${1:-}" == "--start" ]]; then
    systemctl --user enable kanban-rule-distiller.service
    systemctl --user start kanban-rule-distiller.service
    echo "✓ Enabled and started kanban-rule-distiller.service"
    echo ""
    echo "Logs:"
    journalctl --user -u kanban-rule-distiller.service -f
else
    echo ""
    echo "Installation complete. To start the service:"
    echo "  systemctl --user enable kanban-rule-distiller.service"
    echo "  systemctl --user start kanban-rule-distiller.service"
    echo ""
    echo "View logs:"
    echo "  journalctl --user -u kanban-rule-distiller.service -f"
fi
