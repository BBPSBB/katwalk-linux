# Build the KATWALK SteamVR driver (.so). Run 'make' here; needs a C++ toolchain.
#   On an immutable distro, build inside a toolbox/distrobox with gcc + make.
OPENVR ?= openvr/headers
CXX    ?= g++
CXXFLAGS = -std=c++17 -O2 -g -fPIC -Wall -Wextra -isystem $(OPENVR)
# self-contained: don't depend on the host's libstdc++/libgcc (vrserver ABI safety)
LDFLAGS  = -static-libstdc++ -static-libgcc
OUT = katwalk/bin/linux64/driver_katwalk.so

$(OUT): src/driver_katwalk.cpp
	@mkdir -p $(dir $(OUT))
	$(CXX) $(CXXFLAGS) -shared $< -o $@ $(LDFLAGS)
	@echo "built $@"

clean:
	rm -f $(OUT)

.PHONY: clean
