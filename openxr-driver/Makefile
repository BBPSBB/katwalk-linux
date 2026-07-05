# Build the katwalk-linux OpenXR API layer (.so). Run 'make' here; needs a C++ toolchain.
#   On an immutable distro, build inside a toolbox/distrobox with gcc + make.
OPENXR ?= openxr/headers
CXX    ?= g++
CXXFLAGS = -std=c++17 -O2 -g -fPIC -Wall -Wextra -isystem $(OPENXR)
# self-contained: don't depend on the host's libstdc++/libgcc
LDFLAGS  = -static-libstdc++ -static-libgcc
OUT = bin/libkatwalk_xr_layer.so

$(OUT): src/layer.cpp
	@mkdir -p $(dir $(OUT))
	$(CXX) $(CXXFLAGS) -shared $< -o $@ $(LDFLAGS)
	@echo "built $@"

clean:
	rm -f $(OUT)

.PHONY: clean
