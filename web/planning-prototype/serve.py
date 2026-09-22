"""Throwaway, local-only static preview. No controller or file-write endpoints."""

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=4310)
args = parser.parse_args()

if os.environ.get("NODE_ENV") == "production":
    parser.error("This throwaway prototype is not available in production mode.")

handler = partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent))
server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
print(f"Issue 10 prototype: http://127.0.0.1:{server.server_port}/?variant=A", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    server.server_close()
