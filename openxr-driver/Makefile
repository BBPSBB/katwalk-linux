# Build the katwalk-linux OpenXR API layer (.so). Run 'make' here; needs a C++ toolchain.
#   On an immutable distro, build inside a toolbox/distrobox with gcc + make. The HUD path also
#   needs Vulkan + OpenXR-platform headers (vulkan-headers, openxr) in the build env; libvulkan
#   is loaded at runtime via dlopen, so there is no link-time Vulkan dependency.
# -Wno-missing-field-initializers: OpenXR/Vulkan structs are zero-init via {XR_TYPE_x}; the rest
#   of each struct is value-initialized to zero on purpose, so the -Wextra note is just noise.
OPENXR ?= openxr/headers
CXX    ?= g++
# single source of truth: the version literal lives only in katwalk/__init__.py
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' ../katwalk/__init__.py)
CXXFLAGS = -std=c++17 -O2 -g -fPIC -Wall -Wextra -Wno-missing-field-initializers -DKATWALK_VERSION='"$(VERSION)"' -isystem $(OPENXR)
# self-contained: don't depend on the host's libstdc++/libgcc
LDFLAGS  = -static-libstdc++ -static-libgcc
OUT = bin/libkatwalk_xr_layer.so

$(OUT): src/layer.cpp src/katwalk_shm.h
	@mkdir -p $(dir $(OUT))
	@printf '#include <vulkan/vulkan.h>\n' | $(CXX) -fsyntax-only -x c++ - 2>/dev/null || { \
	  echo ""; \
	  echo "ERROR: <vulkan/vulkan.h> not found - the HUD build needs Vulkan headers."; \
	  echo "Install the 'vulkan-headers' and 'openxr' dev packages, or build inside a"; \
	  echo "toolbox/container that provides them."; \
	  echo ""; \
	  exit 1; }
	$(CXX) $(CXXFLAGS) -shared $< -o $@ $(LDFLAGS)
	@echo "built $@"

clean:
	rm -f $(OUT)

.PHONY: clean
