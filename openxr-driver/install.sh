#!/usr/bin/env bash
# Install the katwalk-linux OpenXR API layer as a per-user IMPLICIT layer, so it loads
# into every OpenXR game (native AND Proton) and injects treadmill locomotion into the
# left-thumbstick action the game already reads.
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SO_SRC="$SCRIPT_DIR/bin/libkatwalk_xr_layer.so"
LAYER_NAME="XR_APILAYER_KATWALK_locomotion"
IMPLICIT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openxr/1/api_layers/implicit.d"
SO_DST="$IMPLICIT_DIR/libkatwalk_xr_layer.so"
MANIFEST="$IMPLICIT_DIR/katwalk_locomotion.json"

echo -e "${BLUE}🚀 Installing katwalk-linux OpenXR locomotion layer${NC}"

# 1. the layer must be built
if [ ! -f "$SO_SRC" ]; then
    echo -e "${RED}✗ layer not built:${NC} $SO_SRC"
    echo -e "${YELLOW}  build it first:${NC} run 'make' in this directory"
    exit 1
fi
echo -e "${GREEN}✓ built layer found${NC}"

# 2. install the .so NEXT TO the manifest and reference it with a RELATIVE path. The
#    OpenXR loader resolves a relative library_path against the manifest's own dir, so it
#    works inside Proton's pressure-vessel sandbox where absolute host paths may not map.
mkdir -p "$IMPLICIT_DIR"
install -m 0644 "$SO_SRC" "$SO_DST"
echo -e "${GREEN}✓ installed library${NC} -> $SO_DST"

# 3. write the implicit-layer manifest
cat >"$MANIFEST" <<EOF
{
    "file_format_version": "1.0.0",
    "api_layer": {
        "name": "$LAYER_NAME",
        "library_path": "./libkatwalk_xr_layer.so",
        "api_version": "1.0",
        "implementation_version": "1",
        "description": "katwalk-linux treadmill locomotion injection",
        "disable_environment": "DISABLE_KATWALK_XR_LAYER"
    }
}
EOF
echo -e "${GREEN}✓ installed manifest${NC} -> $MANIFEST"

echo -e "${BLUE}✨ Done.${NC}"
echo -e "${YELLOW}  • Start (or restart) the OpenXR game - the layer attaches at app launch.${NC}"
echo -e "${YELLOW}  • Verify it loaded:${NC} grep -l libkatwalk_xr_layer /proc/*/maps"
echo -e "${YELLOW}  • Turn it off without uninstalling:${NC} export DISABLE_KATWALK_XR_LAYER=1"
echo -e "${YELLOW}  • Rebuilt the layer? Re-run this script to copy the new build.${NC}"
