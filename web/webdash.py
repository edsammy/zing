import serial
import serial.tools.list_ports
import asyncio
import json
import sys
import time
import argparse
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BAUD = 115200
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
BUF_MAXLEN = 2000

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_data_buf = deque(maxlen=BUF_MAXLEN)
_broadcast_event = asyncio.Event()
_web_host = WEB_HOST
_web_port = WEB_PORT
_last_print_time = 0
_print_interval = 0.5  # print live value every 500ms


def _format_live(d):
    return (
        f"I={d['i']:>9.6f}A  "
        f"V={d['v']:>6.3f}V  "
        f"P={d['p']:>7.3f}W  "
        f"Q={d['q']:>9.6f}C  "
        f"E={d['e']:>9.6f}J  "
        f"t={d['t']}ms"
    )

# ---------------------------------------------------------------------------
# Serial auto-detect (same as logger.py)
# ---------------------------------------------------------------------------

def find_pico_port():
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if any(x in desc for x in ["pico", "rp2040", "board"]) or \
           any(x in hwid for x in ["2e8a:000a", "2e8a"]):
            return p.device
    candidates = [p.device for p in ports
                  if "/ttyACM" in p.device or "/ttyUSB" in p.device
                  or "usbmodem" in p.device.lower()]
    if candidates:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Data buffer helpers
# ---------------------------------------------------------------------------

def buf_push(d):
    def _num(key, default):
        v = d.get(key, default)
        return v if v is not None else default
    _data_buf.append((
        _num("t", 0),
        _num("i", 0.0),
        _num("v", 0.0),
        _num("p", 0.0),
        _num("q", 0.0),
        _num("e", 0.0),
    ))


def buf_get_series(since_ms=0):
    if not _data_buf:
        return []
    # Detect source reboot: newest timestamp is older than last sent
    if _data_buf[-1][0] < since_ms:
        return [
            {"x": t, "i": i, "v": v, "p": p, "q": q, "e": e}
            for t, i, v, p, q, e in _data_buf
        ]
    return [
        {"x": t, "i": i, "v": v, "p": p, "q": q, "e": e}
        for t, i, v, p, q, e in _data_buf
        if t > since_ms
    ]


# ---------------------------------------------------------------------------
# Async serial reader
# ---------------------------------------------------------------------------

async def serial_reader(explicit_port, baud):
    raw_buf = b""
    while True:
        port = explicit_port or find_pico_port()
        if not port:
            print("No serial port found — retrying in 2s", file=sys.stderr)
            await asyncio.sleep(2)
            continue

        try:
            ser = serial.Serial(port, baud, timeout=0)
        except serial.SerialException as e:
            print(f"Serial: {e} — retrying in 2s", file=sys.stderr)
            await asyncio.sleep(2)
            continue

        _data_buf.clear()
        print(f"Connected to {port} at {baud} baud.", file=sys.stderr)

        while True:
            try:
                n = ser.in_waiting
            except (serial.SerialException, OSError):
                break

            if n:
                try:
                    raw_buf += ser.read(n)
                except (serial.SerialException, OSError):
                    break

                while b"\n" in raw_buf:
                    line, raw_buf = raw_buf.split(b"\n", 1)
                    text = line.decode().strip()
                    if not text:
                        continue
                    try:
                        d = json.loads(text)
                        if "error" in d:
                            print(f"ERROR: {d['error']}", file=sys.stderr)
                            continue
                        buf_push(d)
                        _broadcast_event.set()
                        global _last_print_time
                        now = time.time()
                        if sys.stdout.isatty() and now - _last_print_time >= _print_interval:
                            sys.stdout.write("\r" + " " * 80 + "\r")
                            sys.stdout.write(_format_live(d))
                            sys.stdout.flush()
                            _last_print_time = now
                    except json.JSONDecodeError:
                        pass
            else:
                await asyncio.sleep(0.005)

        try:
            ser.close()
        except Exception:
            pass
        raw_buf = b""
        print("Serial lost — reconnecting...", file=sys.stderr)
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# HTTP / SSE server
# ---------------------------------------------------------------------------

# Load HTML from file so we don't embed 200 lines of markup in Python
_HTML_PATH = Path(__file__).parent / "index.html"
DASHBOARD_HTML = _HTML_PATH.read_text() if _HTML_PATH.exists() else (
    "<html><body><h1>Dashboard HTML not found.</h1></body></html>"
)


async def http_handle(reader, writer):
    request = await reader.read(4096)
    lines = request.decode().split("\r\n")
    if not lines:
        writer.close()
        return
    req_line = lines[0]
    parts = req_line.split()
    if len(parts) < 2:
        writer.close()
        return
    method, path = parts[0], parts[1]

    if path == "/" or path == "/index.html":
        body = DASHBOARD_HTML.encode()
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(headers.encode() + body)
        await writer.drain()
        writer.close()

    elif path == "/stream":
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n\r\n"
        )
        writer.write(headers.encode())
        await writer.drain()

        last_ts = 0
        try:
            while True:
                try:
                    await asyncio.wait_for(_broadcast_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                _broadcast_event.clear()

                series = buf_get_series(last_ts)
                if series:
                    payload = json.dumps(series)
                    writer.write(f"data: {payload}\n\n".encode())
                    await writer.drain()
                    last_ts = series[-1]["x"]
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    else:
        # Try to serve static files from the web/ directory
        safe_path = path.lstrip('/')
        if '..' in safe_path:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        file_path = Path(__file__).parent / safe_path
        if file_path.exists() and file_path.is_file():
            body = file_path.read_bytes()
            content_type = "application/javascript" if safe_path.endswith(".js") else "application/octet-stream"
            headers = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(headers.encode() + body)
            await writer.drain()
            writer.close()
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()


async def http_server():
    server = await asyncio.start_server(http_handle, _web_host, _web_port)
    display_host = "localhost" if _web_host == "0.0.0.0" else _web_host
    print(f"Dashboard running at http://{display_host}:{_web_port}/", file=sys.stderr)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="INA228 web dashboard")
    parser.add_argument("--port", help="Serial port (auto-detect if not specified)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help=f"Web dashboard port (default: {WEB_PORT})")
    parser.add_argument("--web-host", default=WEB_HOST, help=f"Web dashboard host (default: {WEB_HOST})")
    args = parser.parse_args()

    if args.port and not find_pico_port():
        # Sanity check: explicit port may not exist yet, but that's ok —
        # serial_reader will retry until it appears.
        pass

    global _web_host, _web_port
    _web_host = args.web_host
    _web_port = args.web_port

    async def run_all():
        await asyncio.gather(
            serial_reader(args.port, args.baud),
            http_server(),
        )

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()
        print("Stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
