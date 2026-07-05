#!/usr/bin/env bash
# Unregister the katwalk-linux SteamVR treadmill driver from SteamVR.
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
echo -e "${BLUE}🗑️  katwalk-linux SteamVR driver - uninstall${NC}"

DRIVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/katwalk" && pwd)"

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

echo -e "${YELLOW}• removing driver:${NC} $DRIVER_DIR"
"$VRPATHREG" removedriver "$DRIVER_DIR"
echo -e "${GREEN}✓ unregistered.${NC} Restart SteamVR to fully unload."
