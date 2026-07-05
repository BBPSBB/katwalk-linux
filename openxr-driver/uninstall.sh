#!/usr/bin/env bash
# Remove the katwalk-linux OpenXR API layer (reverses install.sh).
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

IMPLICIT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openxr/1/api_layers/implicit.d"
SO_DST="$IMPLICIT_DIR/libkatwalk_xr_layer.so"
MANIFEST="$IMPLICIT_DIR/katwalk_locomotion.json"

echo -e "${BLUE}🗑️  Uninstalling katwalk-linux OpenXR locomotion layer${NC}"

removed=0
if [ -f "$MANIFEST" ]; then
  rm -f "$MANIFEST"
  echo -e "${GREEN}✓ removed manifest${NC}"
  removed=1
fi
if [ -f "$SO_DST" ]; then
  rm -f "$SO_DST"
  echo -e "${GREEN}✓ removed library${NC}"
  removed=1
fi

if [ "$removed" -eq 0 ]; then
  echo -e "${YELLOW}nothing to remove (was not installed).${NC}"
else
  echo -e "${BLUE}✨ Done.${NC} The layer will no longer load into OpenXR games."
fi
