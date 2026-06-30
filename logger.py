import serial
import serial.tools.list_ports
import csv
import sys
import json
import argparse
import time

DEFAULT_BAUD = 115200
DEFAULT_OUTPUT = "power.csv"


def find_pico_port():
    """Auto-detect Pico USB CDC port."""
    ports = list(serial.tools.list_ports.comports())
    
    # Look for common Pico identifiers
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if any(x in desc for x in ["pico", "rp2040", "board"]) or \
           any(x in hwid for x in ["2e8a:000a", "2e8a"]):
            return p.device
    
    # Fallback: look for ttyACM/ttyUSB/cu.usbmodem
    candidates = [p.device for p in ports 
                  if "/ttyACM" in p.device or "/ttyUSB" in p.device 
                  or "usbmodem" in p.device.lower()]
    if candidates:
        return candidates[0]
    
    return None


def clear_line():
    """Clear current terminal line."""
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


def format_live(d):
    """Format a single-line live dashboard."""
    return (
        f"I={d['i']:>9.6f}A  "
        f"V={d['v']:>6.3f}V  "
        f"P={d['p']:>7.3f}W  "
        f"Q={d['q']:>9.6f}C  "
        f"E={d['e']:>9.6f}J  "
        f"t={d['t']}ms"
    )


def main():
    parser = argparse.ArgumentParser(description="INA228 current monitor logger")
    parser.add_argument("--port", help="Serial port (auto-detect if not specified)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output CSV file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--raw", action="store_true", help="Stream raw JSON even in a terminal")
    args = parser.parse_args()

    port = args.port or find_pico_port()
    if not port:
        print("No serial port found. Available ports:", file=sys.stderr)
        for p in serial.tools.list_ports.comports():
            print(f"  {p.device}: {p.description}", file=sys.stderr)
        sys.exit(1)

    use_live = sys.stdout.isatty() and not args.raw

    print(f"Opening {port} at {args.baud} baud...", file=sys.stderr)
    ser = serial.Serial(port, args.baud, timeout=1)

    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ms", "current_A", "voltage_V", "power_W", "charge_C", "energy_J"])
        print(f"Logging to {args.output}. Press Ctrl+C to stop.", file=sys.stderr)

        if use_live:
            print("Live view enabled. Use --raw for JSON stream.", file=sys.stderr)

        last_display = 0
        display_interval = 0.1  # Update live view at 10 Hz

        for line in ser:
            try:
                d = json.loads(line)
                if "error" in d:
                    if use_live:
                        clear_line()
                    print(f"ERROR: {d['error']}", file=sys.stderr)
                    if use_live:
                        sys.stdout.write(format_live(d) + "  [ERROR]\n")
                    continue

                w.writerow([d["t"], d["i"], d["v"], d["p"], d["q"], d["e"]])
                f.flush()

                if use_live:
                    now = time.time()
                    if now - last_display >= display_interval:
                        clear_line()
                        sys.stdout.write(format_live(d))
                        sys.stdout.flush()
                        last_display = now
                else:
                    sys.stdout.write(line.decode().strip() + "\n")
                    sys.stdout.flush()

            except (json.JSONDecodeError, KeyError):
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            clear_line()
        print("\nStopped.", file=sys.stderr)
        sys.exit(0)
    except serial.SerialException as e:
        print(f"Serial error: {e}", file=sys.stderr)
        sys.exit(1)
