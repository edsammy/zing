#!/bin/bash
set -e

# Build script for current_monitor firmware
# This handles the CMake 3.31 + ARM toolchain setup automatically

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/firmware/out"

# --- Configuration ---
export PICO_SDK_PATH="${PICO_SDK_PATH:-$HOME/pico/pico-sdk}"
export PICO_TOOLCHAIN_PATH="${PICO_TOOLCHAIN_PATH:-/Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi}"

# Use downloaded CMake 3.31 to avoid pico-sdk incompatibility with CMake 4.x
CMAKE_BIN="/tmp/cmake-3.31.6-macos-universal/CMake.app/Contents/bin/cmake"
if [ ! -x "$CMAKE_BIN" ]; then
    echo "ERROR: CMake 3.31 not found at $CMAKE_BIN"
    echo "Download it from: https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6-macos-universal.tar.gz"
    echo "Then extract to /tmp/"
    exit 1
fi

# Check ARM toolchain
if [ ! -x "$PICO_TOOLCHAIN_PATH/bin/arm-none-eabi-gcc" ]; then
    echo "ERROR: ARM toolchain not found at $PICO_TOOLCHAIN_PATH"
    echo "Install with: brew install gcc-arm-embedded"
    exit 1
fi

# Check SDK
if [ ! -d "$PICO_SDK_PATH" ]; then
    echo "ERROR: pico-sdk not found at $PICO_SDK_PATH"
    echo "Clone it: git clone https://github.com/raspberrypi/pico-sdk.git ~/pico/pico-sdk"
    exit 1
fi

# --- Build ---
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# Run cmake only if needed (first build, or CMakeLists.txt changed)
if [ ! -f "$BUILD_DIR/Makefile" ] || [ "$SCRIPT_DIR/CMakeLists.txt" -nt "$BUILD_DIR/Makefile" ]; then
    echo "==> Running cmake..."
    "$CMAKE_BIN" \
        -DPICO_SDK_PATH="$PICO_SDK_PATH" \
        -DPICO_TOOLCHAIN_PATH="$PICO_TOOLCHAIN_PATH" \
        ..
fi

echo "==> Building..."
make -j$(sysctl -n hw.ncpu)

echo ""
echo "==> Build complete: $BUILD_DIR/current_monitor.uf2"
echo "Flash to Pico: hold BOOTSEL, plug in USB, drag $BUILD_DIR/current_monitor.uf2 to the RPI-RP2 drive"
