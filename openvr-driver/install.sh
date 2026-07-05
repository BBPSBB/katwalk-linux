#!/usr/bin/env bash
# Register the katwalk-linux SteamVR treadmill driver with SteamVR (vrpathreg).
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
echo -e "${BLUE}🚀 katwalk-linux SteamVR driver - install${NC}"

DRIVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/katwalk" && pwd)"
SO="$DRIVER_DIR/bin/linux64/driver_katwalk.so"

if [ ! -f "$SO" ]; then
    echo -e "${RED}✗ driver not built:${NC} $SO"
    echo -e "${YELLOW}  build it first:${NC} run 'make' in the driver directory"
    exit 1
fi

VRPATHREG=""
for c in \
    "$HOME/.local/share/Steam/steamapps/common/SteamVR/bin/vrpathreg.sh" \
    "$HOME/.steam/steam/steamapps/common/SteamVR/bin/vrpathreg.sh" \
    "$HOME/.steam/root/steamapps/common/SteamVR/bin/vrpathreg.sh"; do
    [ -x "$c" ] && VRPATHREG="$c" && break
done
if [ -z "$VRPATHREG" ]; then
    echo -e "${RED}✗ vrpathreg not found - is SteamVR installed?${NC}"
    exit 1
fi

echo -e "${YELLOW}• vrpathreg:${NC} $VRPATHREG"
echo -e "${YELLOW}• driver:   ${NC} $DRIVER_DIR"
"$VRPATHREG" adddriver "$DRIVER_DIR"

echo -e "${GREEN}✓ registered.${NC} Restart SteamVR, then bind ${BLUE}katwalk-linux${NC} (treadmill) to the game's Walk action."
echo -e "${YELLOW}  drive it:${NC} python3 -m katwalk.daemon"
