import serial
import serial.tools.list_ports
import asyncio
import json
import sys
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
    _data_buf.append((
        d.get("t", 0),
        d.get("i", 0.0),
        d.get("v", 0.0),
        d.get("p", 0.0),
        d.get("q", 0.0),
        d.get("e", 0.0),
    ))


def buf_get_series(since_ms=0):
    return [
        {"x": t, "i": i, "v": v, "p": p, "q": q, "e": e}
        for t, i, v, p, q, e in _data_buf
        if t > since_ms
    ]


# ---------------------------------------------------------------------------
# Async serial reader
# ---------------------------------------------------------------------------

async def serial_reader(port, baud):
    loop = asyncio.get_event_loop()
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except serial.SerialException as e:
        print(f"Serial error: {e}", file=sys.stderr)
        return

    print(f"Connected to {port} at {baud} baud.", file=sys.stderr)

    def read_line():
        line = ser.readline()
        return line.decode().strip() if line else None

    while True:
        line = await loop.run_in_executor(None, read_line)
        if not line:
            await asyncio.sleep(0.001)
            continue
        try:
            d = json.loads(line)
            if "error" in d:
                print(f"ERROR: {d['error']}", file=sys.stderr)
                continue
            buf_push(d)
            _broadcast_event.set()
        except json.JSONDecodeError:
            pass


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

    serial_port = args.port or find_pico_port()
    if not serial_port:
        print("No serial port found.", file=sys.stderr)
        sys.exit(1)

    global _web_host, _web_port
    _web_host = args.web_host
    _web_port = args.web_port

    async def run_all():
        await asyncio.gather(
            serial_reader(serial_port, args.baud),
            http_server(),
        )

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
