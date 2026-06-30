#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UF2="$SCRIPT_DIR/firmware/out/current_monitor.uf2"

if [ ! -f "$UF2" ]; then
    echo "ERROR: UF2 not found at $UF2"
    echo "Build first: ./build.sh"
    exit 1
fi

# Try picotool first (no buttons needed!)
if command -v picotool &> /dev/null; then
    echo "Flashing with picotool (no buttons needed)..."
    picotool load "$UF2" -f
    echo "Flash complete! Pico rebooting..."
    exit 0
fi

# Fallback: manual BOOTSEL drag-and-drop
MOUNT=""
for dir in /Volumes/RPI-RP2 /Volumes/RPI-RP* /media/*/RPI-RP2; do
    if [ -d "$dir" ]; then
        MOUNT="$dir"
        break
    fi
done

if [ -z "$MOUNT" ]; then
    echo "ERROR: Pico not found in BOOTSEL mode."
    echo "Install picotool for button-free flashing: brew install picotool"
    echo "Or hold BOOTSEL and plug in the USB cable."
    exit 1
fi

echo "Flashing $UF2 to Pico ($MOUNT)..."
cp "$UF2" "$MOUNT/"
sleep 1

if [ ! -d "$MOUNT" ]; then
    echo "Flash complete! Pico will reboot automatically."
else
    echo "Copy complete. Unmount $MOUNT manually if needed."
fi
