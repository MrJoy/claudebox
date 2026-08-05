#!/usr/bin/env python3
"""Loopback normalizer that sits between LiteLLM and Cloudflare's AI REST API.

Claude Code -> LiteLLM (:LITELLM_PORT) -> this (:SHIM_PORT) -> api.cloudflare.com

It exists for one defect that cannot be fixed with LiteLLM configuration:
LiteLLM omits the ``content`` key entirely on an assistant message that carries
only ``tool_calls``. The OpenAI API permits that; some Cloudflare-served models
do not. Kimi rejects it with

    Model execution failed (User Input Error):
    Invalid value at messages[N].content: Invalid input

Verified against Cloudflare directly: the identical request with ``content: ""``
added to that message returns 200. Claude Code emits a tool-only assistant turn
on every tool call, so without this fix such a model fails on essentially every
review. So: fill in ``content: ""`` where it is missing, change nothing else, and
forward. Adding an empty string is valid OpenAI in its own right, which is why
this runs for every model rather than being switched on per model -- one code
path that is always exercised beats a rarely-tested conditional.

Deliberately dependency-free (stdlib only): it sits in the request path holding a
credential, so its whole surface should be readable in one sitting. It is NOT a
general proxy -- the upstream is fixed at startup from the environment, so
nothing in a request can retarget it at another host.
"""

import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fixed at startup. A request cannot influence where this forwards to.
UPSTREAM = os.environ.get("SHIM_UPSTREAM_URL", "")
PORT = int(os.environ.get("SHIM_PORT", "4001"))
# Response headers we must not copy: they describe OUR connection to the client,
# not the upstream's to us, and passing them through corrupts the framing.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def normalize(payload):
    """Fill in a missing/null assistant ``content``. Returns True if changed."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        # Only when absent or null. An existing "" or a real string or list is
        # left exactly as it is -- this fixes one shape, it does not reformat
        # anyone's messages.
        if message.get("content") is None:
            message["content"] = ""
            changed = True
    return changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Quiet: one line per request would interleave with the reviewer's own log
    # for no benefit. Errors still surface, via log_error below.
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        sys.stderr.write("shim: " + (fmt % args) + "\n")
        sys.stderr.flush()

    def do_GET(self):
        # Startup probe only. entrypoint.sh blocks on this before it starts
        # LiteLLM in front of us, so a request never arrives before we can serve
        # it. Deliberately not a proxied request: it must not need a credential.
        self.send_response(204)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        # A body we cannot parse is forwarded untouched rather than rejected:
        # this exists to fix one field, not to validate anyone's requests.
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and normalize(payload):
                body = json.dumps(payload).encode()
        except (ValueError, UnicodeDecodeError):
            pass

        target = urllib.parse.urlparse(UPSTREAM)
        base = target.path.rstrip("/")
        # SHIM_UPSTREAM_URL carries Cloudflare's full base path (ending /ai/v1);
        # LiteLLM contributes the endpoint (/chat/completions). LiteLLM has been
        # seen to prepend /v1 itself depending on how api_base is spelled, so
        # collapse the overlap rather than producing /ai/v1/v1/chat/completions,
        # which Cloudflare answers with a bare "No route for that URI".
        suffix = self.path
        if base.endswith("/v1") and suffix.startswith("/v1/"):
            suffix = suffix[3:]
        path = base + suffix

        # Pass the client's headers through -- notably Authorization, so the
        # Cloudflare token lives in LiteLLM's config and not in this process.
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
        }
        headers["Content-Length"] = str(len(body))

        try:
            if target.scheme == "https":
                conn = http.client.HTTPSConnection(target.netloc, timeout=900)
            else:
                conn = http.client.HTTPConnection(target.netloc, timeout=900)
            conn.request("POST", path, body=body, headers=headers)
            upstream = conn.getresponse()
        except OSError as exc:
            self.log_error("upstream connection failed: %s", exc)
            self.send_response(502)
            self.send_header("content-type", "application/json")
            payload = json.dumps({"error": {"message": "shim: upstream connection failed",
                                            "type": "shim_upstream_error"}}).encode()
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(upstream.status)
        for key, value in upstream.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        # Always chunked: a streamed completion has no length up front, and
        # re-deriving one would mean buffering the whole response, which would
        # turn token streaming into one long stall.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                # read1, NOT read: read(n) blocks until it has all n bytes or the
                # response ends, which buffers a streamed completion into a single
                # lump delivered at the end. Verified against a chunked server --
                # read() turned 0s/1s/2s/3s arrivals into everything at 3s, i.e.
                # Claude Code sitting silent for a whole generation.
                chunk = upstream.read1(8192)
                if not chunk:
                    break
                # Flush every chunk: buffering here would defeat streaming.
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The client gave up mid-stream (a cancelled review). Not an error.
            pass
        finally:
            conn.close()


def main():
    if not UPSTREAM:
        sys.exit("shim: SHIM_UPSTREAM_URL is required")
    parsed = urllib.parse.urlparse(UPSTREAM)
    # https, or plain http to loopback. The Authorization header travels on this
    # hop, so cleartext is only acceptable when the hop does not leave the host --
    # which is what makes it possible to point this at a local echo server and
    # capture the exact outbound body without needing a real Cloudflare token.
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("shim: SHIM_UPSTREAM_URL must be https (http is allowed only to loopback)")
    # 127.0.0.1 only: this forwards a credential and must not be reachable off
    # the container, exactly as with the LiteLLM proxy in front of it.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("shim: listening on 127.0.0.1:%d -> %s\n" % (PORT, UPSTREAM))
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
