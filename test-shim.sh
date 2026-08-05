#!/usr/bin/env bash
# Tests workersai-shim.py, the normalizer the entrypoint runs between LiteLLM and
# Cloudflare for PROVIDER=workersai.
#
# Complements test-providers.sh rather than overlapping it: that suite stubs
# python3 and proves the chain is *wired* correctly, so nothing there exercises a
# single line of the normalizer. This runs the real thing against a real local
# echo server -- no Docker, no network, no credentials -- and checks the three
# things that have to be right:
#
#   1. the content injection it exists for, and its restraint (nothing else edited)
#   2. streaming relayed incrementally, not buffered to the end of the response
#   3. loopback-only bind, and no cleartext hop off the host
#
# (2) is here because it was a real bug, not a hypothetical: the first version used
# read() instead of read1() and delivered a whole streamed completion in one lump
# at the end, which looks exactly like a hung reviewer.
set -u
cd "$(dirname "$0")"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s\n       %s\n' "$1" "$2"; }

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

# One python process runs the echo servers, the shim under test, and the raw-socket
# client. Doing it in-process keeps the timing measurements honest -- shelling out
# per line costs ~0.3s, which is enough to hide the very buffering bug (2) checks.
OUT=$(python3 - <<'PY' 2>&1
import json, os, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

# --- an upstream that records what it was handed --------------------------------
captured = {}
class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        captured["body"] = self.rfile.read(n)
        captured["path"] = self.path
        captured["auth"] = self.headers.get("authorization")
        r = json.dumps({"object": "chat.completion"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(r)))
        self.end_headers()
        self.wfile.write(r)

# --- an upstream that streams, one event per second -----------------------------
class Stream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for i in range(3):
            d = b'data: {"i":%d}\n\n' % i
            self.wfile.write(b"%x\r\n%s\r\n" % (len(d), d)); self.wfile.flush()
            time.sleep(1)
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()

def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]

echo_port, stream_port = serve(Echo), serve(Stream)

def start_shim(upstream):
    """Start the real shim and wait for its probe to answer. Returns (proc, port)."""
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ, SHIM_UPSTREAM_URL=upstream, SHIM_PORT=str(port))
    p = subprocess.Popen([sys.executable, "workersai-shim.py"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return p, port
        except OSError:
            if p.poll() is not None:
                return p, port
            time.sleep(0.1)
    return p, port

def post(port, body, path="/chat/completions", headers=""):
    c = socket.create_connection(("127.0.0.1", port), timeout=30)
    c.sendall(b"POST %s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n%s"
              b"Content-Length: %d\r\n\r\n%s"
              % (path.encode(), headers.encode(), len(body), body))
    return c

# --- 1. injection, and restraint -----------------------------------------------
shim, port = start_shim("http://127.0.0.1:%d/client/v4/accounts/a/ai/v1" % echo_port)
body = json.dumps({"model": "m", "messages": [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "tool_calls": [{"id": "t1"}]},      # the defect: no content
    {"role": "assistant", "content": None},                   # explicit null
    {"role": "assistant", "content": "keep me"},              # must not be touched
    {"role": "assistant", "content": [{"type": "text", "text": "arr"}]},
    {"role": "tool", "tool_call_id": "t1", "content": "out"},
]}).encode()
c = post(port, body, headers="Authorization: Bearer SEKRIT\r\n")
c.recv(4096); c.close()
sent = json.loads(captured.get("body", b"{}"))
msgs = sent.get("messages", [])
check("a tool-call-only assistant message gets content: ''",
      msgs[1:2] == [{"role": "assistant", "tool_calls": [{"id": "t1"}], "content": ""}],
      repr(msgs[1:2]))
check("an explicit null content is filled in too",
      msgs[2:3] == [{"role": "assistant", "content": ""}], repr(msgs[2:3]))
check("existing string content is left alone",
      msgs[3:4] == [{"role": "assistant", "content": "keep me"}], repr(msgs[3:4]))
check("array-shaped content is left alone",
      msgs[4:5] == [{"role": "assistant", "content": [{"type": "text", "text": "arr"}]}],
      repr(msgs[4:5]))
check("non-assistant messages are left alone",
      msgs[5:6] == [{"role": "tool", "tool_call_id": "t1", "content": "out"}], repr(msgs[5:6]))
check("nothing else in the request is rewritten", sent.get("model") == "m", repr(sent.get("model")))
# The Cloudflare credential rides through on this hop; the shim holds none itself.
check("the Authorization header is passed through",
      captured.get("auth") == "Bearer SEKRIT", repr(captured.get("auth")))
# A doubled /v1 is what Cloudflare answers with a bare "No route for that URI".
check("the upstream base path and the endpoint are joined without a doubled /v1",
      captured.get("path") == "/client/v4/accounts/a/ai/v1/chat/completions", repr(captured.get("path")))

# A body that isn't JSON is forwarded as-is: this fixes one field, it does not get
# to reject requests it doesn't understand.
c = post(port, b"not json at all"); c.recv(4096); c.close()
check("a non-JSON body is forwarded untouched", captured.get("body") == b"not json at all",
      repr(captured.get("body")))
shim.terminate()

# --- 2. streaming is relayed as it arrives -------------------------------------
shim, port = start_shim("http://127.0.0.1:%d/ai/v1" % stream_port)
c = post(port, b'{"stream":true}')
t0 = time.monotonic(); arrivals = []
while True:
    d = c.recv(4096)
    if not d:
        break
    arrivals.append((time.monotonic() - t0, d))
    if d.endswith(b"0\r\n\r\n"):
        break
c.close()
# The upstream emits at 0s, 1s, 2s and ends at 3s. Buffering shows up as every
# event landing together at the end, so what matters is that something arrived
# well before the response was complete.
first_data = next((t for t, d in arrivals if b'data:' in d), None)
last = arrivals[-1][0] if arrivals else 0
check("a streamed response is relayed incrementally, not buffered to the end",
      first_data is not None and last >= 2.0 and first_data < last - 1.0,
      "first data at %s, stream ended at %.2fs" % (
          "never" if first_data is None else "%.2fs" % first_data, last))
check("all four streamed events arrive intact",
      sum(d.count(b"data:") for _, d in arrivals) == 3
      and b"".join(d for _, d in arrivals).endswith(b"0\r\n\r\n"),
      repr([d for _, d in arrivals]))
shim.terminate()

# --- 3. it stays on loopback, in cleartext or otherwise ------------------------
p, _ = start_shim("http://example.com/ai/v1")
err = p.communicate(timeout=10)[1].decode()
check("a cleartext upstream that leaves the host is refused",
      p.returncode != 0 and "must be https" in err, "rc=%s err=%r" % (p.returncode, err[:200]))

env = dict(os.environ, SHIM_PORT="0"); env.pop("SHIM_UPSTREAM_URL", None)
p = subprocess.Popen([sys.executable, "workersai-shim.py"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
err = p.communicate(timeout=10)[1].decode()
check("no upstream configured is refused rather than defaulted",
      p.returncode != 0 and "required" in err, "rc=%s err=%r" % (p.returncode, err[:200]))

# The token travels to this listener, so it must not be reachable off the box.
# Asserted by reading the socket's actual bind address rather than by trying to
# reach it from elsewhere: on a host whose only address is loopback, a
# connectivity probe passes no matter what the shim bound, which makes for a test
# that looks green and checks nothing.
shim, port = start_shim("http://127.0.0.1:%d/ai/v1" % echo_port)
try:
    listing = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a",
                              "-p", str(shim.pid)],
                             capture_output=True, timeout=15).stdout.decode()
except (OSError, subprocess.TimeoutExpired) as exc:
    check("SKIP bind address unverifiable (lsof: %s)" % exc, True)
else:
    lines = [l for l in listing.splitlines() if ":%d" % port in l]
    check("the listener is bound to 127.0.0.1, not to every interface",
          lines and all("127.0.0.1:%d" % port in l for l in lines),
          repr(lines) or "no LISTEN line found for port %d" % port)
shim.terminate()

for label, passed, detail in results:
    print("%s\t%s\t%s" % ("ok" if passed else "FAIL", label, detail))
PY
)

while IFS=$'\t' read -r status label detail; do
  case "$status" in
    ok) ok "$label" ;;
    FAIL) bad "$label" "$detail" ;;
    *) [ -n "$status$label" ] && printf '     | %s\n' "$status$label" ;;
  esac
done <<<"$OUT"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
