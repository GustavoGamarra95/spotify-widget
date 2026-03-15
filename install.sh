#!/usr/bin/env bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Spotify Widget Installer ===${NC}"

# ── System dependencies ────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[1/4] Installing system dependencies...${NC}"
sudo apt install -y \
  python3 python3-gi python3-cairo \
  gir1.2-gtk-3.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  playerctl

# ── Python dependencies ────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[2/4] Installing Python dependencies...${NC}"
pip install --user spotipy

# ── Copy widget ────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[3/4] Installing widget...${NC}"
mkdir -p "$HOME/.config/conky"
cp spotify-widget.py "$HOME/.config/conky/spotify-widget.py"
chmod +x "$HOME/.config/conky/spotify-widget.py"
echo -e "  Widget installed to: ${GREEN}~/.config/conky/spotify-widget.py${NC}"

# ── Config file ────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[4/4] Setting up config...${NC}"
CONFIG_DIR="$HOME/.config/spotify-widget"
CONFIG_FILE="$CONFIG_DIR/config.json"

if [ -f "$CONFIG_FILE" ]; then
  echo -e "  Config already exists at ${GREEN}$CONFIG_FILE${NC} — skipping."
else
  mkdir -p "$CONFIG_DIR"
  cp config.example.json "$CONFIG_FILE"
  echo -e "  Config created at: ${GREEN}$CONFIG_FILE${NC}"
  echo -e "  ${YELLOW}Edit it and add your Spotify credentials before running the widget.${NC}"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}Installation complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit ~/.config/spotify-widget/config.json with your Spotify credentials"
echo "     (get them at https://developer.spotify.com/dashboard)"
echo ""
echo "  2. Run the widget:"
echo "     python3 ~/.config/conky/spotify-widget.py &"
echo ""
echo "  3. (Optional) Add to autostart for your desktop environment."
