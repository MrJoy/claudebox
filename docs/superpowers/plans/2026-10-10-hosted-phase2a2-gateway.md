# Hosted Phase 2a.2: The Gateway's Provider Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** The gateway process: a worker-facing HTTP route to the model provider that checks a per-pair token, overwrites the model, enforces per-pair caps, injects the profile's real credential, streams the response unbuffered, and reports every pair's provider outcome to the control plane over a unix socket.

**Architecture:** `hosted/gwproto.py` is line-delimited JSON over unix sockets, with the per-deployment secret on every message in both directions. `hosted/gateway.py` is the process. The control plane connects to the gateway's socket to send `hello`, `add_token`, `revoke` and `flush`. The gateway connects to the control plane's socket to send `report`s, in order per token, buffered while the control plane is away. A report's sequence number is taken and the report queued in one critical section, so queue order is sequence order. Each token has its own queue, and one thread visits them in turn, so a report the control plane will not acknowledge stalls that token's flush and no other, with one WARN naming it. Workers reach the gateway over HTTP and present a provider token minted by `creds.py` (2a.1). The gateway checks it against rows the control plane handed it, so it holds no records store. Each token row carries the profile it was dispatched under, so a deployment change reaches new jobs without touching running ones. Admission counts a request and reserves its `max_tokens` under a lock before forwarding, so the request cap holds against any client, and output never outruns the token cap. Input tokens are counted when the provider reports them, so a parallel client can pass the token cap by at most `IN_FLIGHT_MAX` requests' worth of input. `deploy_config` gains the two profile keys a hosted `custom` or `anthropic` profile needs, `base_url` and `auth`.

**Tech Stack:** Python 3.9+ standard library (`http.server`, `http.client`, `ssl`, `socket`, `threading`, `json`, `unittest`).

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "The gateway" (provider route, author of every pair's provider outcome, what counts as a provider failure), "Deployment config" (budget units, caps), "Secrets" (the gateway holds provider credentials and not the App key), and "Configuration" (the provider types). Overview: `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`, decisions 8 and 9 and the gateway socket protocol. Both are copied under Global Constraints where this plan depends on them.

**Prerequisite:** the 2a.1 foundations plan (PR #18) is built. `gateway.py` imports `creds.verify_against` and `creds.credential_id_of`, and the tests mint tokens with `creds.Creds` over `records.Records`.

## Global Constraints

- **Standard library only.** No module in this plan imports `cryptography`.
- **Python 3.9 syntax** (overview decision 15). Flat modules, imported by plain name (overview decision 19).
- **2a.2 serves `ollama`, `anthropic` and `custom`** (overview decision 8). `cloudflare` and `workersai` profiles load in `config validate`, and the gateway refuses them by name: "not served by the hosted gateway until phase 2a.6".
- **`base_url`** is required on a `custom` profile and refused on every other. **`auth`** is `api_key` or `oauth_token` on `anthropic` (default `api_key`), `bearer` or `x_api_key` on `custom` (required), and refused elsewhere (overview decision 8).
- **Spike S3** (overview decision 9): for an `oauth_token` profile the gateway swaps only the bearer value, and passes `anthropic-beta` through. Claude Code's `HEAD /api/hello` is answered with 200 and no upstream call.
- The spec's route: it "Overwrites `model` with the job's model on every request. For Anthropic-shaped profiles it allows `POST /v1/messages` and `POST /v1/messages/count_tokens` and nothing else".
- The spec's caps: "Where the provider reports usage it counts input plus output tokens per pair, clamps `max_tokens` to what remains, and caps body size; where it does not, it caps requests, body size and `max_tokens`". The body cap is "4 MB | fixed | request refused". "Hitting one ends the pair as `capped`."
- The spec's record: "successes, tokens, the first usage limit, the first failure, a cap hit". "The gateway enforces per-pair caps from its own counts, so provider traffic never waits on the control plane". "If the control plane is unreachable, the gateway keeps serving within the caps and buffers its reports".
- The spec's start: "Until it has them, a starting gateway serves no provider traffic, so a restart never resets a cap or loses a pair's attribution." Read with the spec's own bound on a crash: "What a gateway crash can lose is the reports not yet acknowledged, a window the per-pair caps bound." So a restart resumes from the committed counts, and loses whatever was spent and not yet acknowledged, which is at most one per-pair cap per token.
- The spec's flush: "Reports carry a per-token sequence number and the flush answers with the last one sent, so the control plane knows when it holds every report". The control plane treats a flush that "cannot complete within a minute" as a claudebox fault.
- The spec's deployment change: it "takes effect without a restart: jobs already running keep the config they were dispatched with, new jobs get the new one".
- The spec's record holds "only what upstream did, as the gateway saw it", and a failure inside claudebox "never moves the profile's failure counter".
- The overview's gateway protocol (as revised in review): each token carries its `route`, `hello` is a non-regressing snapshot, every other op before a `hello` is answered `{error: "hello first", ready: false}`, `flush` answers `{last_seq, drained}`, and a report may carry `first_rejected`.
- The spec's usage rule: "If a profile set to `tokens` gets a response without usage, the gateway fails that pair closed and pages".
- The spec's socket: the two processes talk "over a unix socket ... and every message in both directions carries a per-deployment secret". A worker cannot reach it.
- "The gateway streams. It does not buffer, cache or log any body, and it does not retry for the client."
- Every existing suite still passes. `tests/test_deploy_config.py` gains cases, and its custom-profile fixtures gain the two keys that are now required. No other existing test changes.

## Decided here

The spec and the overview leave these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **Two sockets, each with one direction of request and reply.** The gateway listens on `GATEWAY_SOCKET` for the control plane's `hello`, `add_token`, `revoke` and `flush`. The control plane listens on `CONTROL_SOCKET` for the gateway's `report`. Each side is a plain client of the other's server, so no connection ever carries requests both ways. Both socket files are mode 0660.
2. **A token carries its profile.** A token row is `{credential_id, hash_hex, salt_hex, job_id, persona, model, expires_at, route: {name, provider, base_url, auth, credential, per_pair_requests, per_pair_tokens, budget_unit}, counts}`. `credential` is the secret's name, and the gateway reads its value from its own environment at request time. A token whose route cannot be served (an unserved provider, or a credential the gateway's environment lacks) is refused on its own, with a `WARN` line and an entry in the `hello` reply's `refused` list, using `served_problems`'s wording. Every other token is still served.
3. **`hello` is a full, non-regressing snapshot.** A token the gateway already holds keeps whichever copy has the higher `seq`, and a token the snapshot leaves out is dropped (the control plane revoked it). Before its first `hello` the gateway answers every other op with `{error: "hello first", ready: false}`, and the control plane (2a.4) answers that with a `hello`. That is how a gateway that restarted alone gets its tokens back. `add_token` for a token already held changes nothing.
4. **Admission is atomic per token, and bounded.** Under the lock, a request is refused if a cap was already hit, if the token has used its request cap, or if its counted tokens have reached the token cap. Those three are cap refusals. It is paced instead (HTTP 429, nothing recorded, nothing concluded) if the token has `IN_FLIGHT_MAX` (8) requests in flight, or if what is left of the token cap is all reserved by requests still running. Otherwise one request is counted and `max_tokens` is reserved, both before the forward. Concurrent requests serialize on the check, so the request cap holds against a client that is not Claude Code. Input is counted when the provider reports it, so the token cap can be passed by at most `IN_FLIGHT_MAX` requests' worth of input. The reservation is released on every path from admission to the reply, an unlisted exception included: `do_POST` wraps the forward in a `finally` that completes the request as `abandoned` with `gateway fault: <type>` and logs a `WARN` when nothing else did.
5. **A cap refusal is HTTP 400 with an `invalid_request_error` body** naming the cap. Claude Code retries a 429 or a 5xx, and a 429 would also read as a usage limit. A cap hit is terminal: every later request on that token is refused with a 400 that says the pair is done, and reports nothing. A cap hit therefore means counted use reached a cap, never a shortfall caused by reservations, which is pacing.
6. **The body cap refuses the request, and is not a pair outcome**, as the spec's cap table has it: "provider request body | 4 MB | fixed | request refused". The 413 is sent on the declared `Content-Length`, before the body is read.
7. **A starting gateway answers 503**, and that refusal is in no record: the spec's "a request ... a starting gateway turned away" is a claudebox fault.
8. **Tokens are counted as every kind of input the provider reports** (`input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`) plus the largest `output_tokens` seen. Output is cumulative across `message_delta`s, and summing it would double-count.
9. **Classification.** HTTP 429 or 529, or an error event of type `rate_limit_error` or `overloaded_error`, is a usage limit. A 401 or 403 is a failure: the provider refused the deployment's credential, which the operator must hear about. A 5xx is a failure. Any other 4xx is `rejected`. The request body is the worker's, so a rejected request is reported as `first_rejected` and never moves the profile's failure counter, which a worker could otherwise drive to park the profile for every repo on it. A transport error reaching upstream, or a read that breaks mid-response, is a failure named `upstream transport: <exception type>`. A 2xx stream that ends without `message_stop` is a failure (`the stream ended before message_stop`). A 2xx on `/v1/messages` is a success. A 2xx on `count_tokens` is neutral: it counts as a request, never as a pass.
10. **A worker that hangs up mid-response** stops the relay. The request and the tokens seen so far are counted, and the report carries `first_abandoned`, which takes the place of `ok` and nothing else (overview decision 2): a record that would otherwise decide `ok` decides `deferred`, and every other outcome, a cap hit's `capped` included, stands over it. The worker chooses when to hang up, so the marker must never turn a cap into a retry, nor hide what upstream answered: a hang-up after a non-2xx status line is recorded as that status's outcome (a limit, a failure, a rejection), since the status alone decides it. Only a lost 2xx, whose outcome needs the body, is abandoned. On a tokens profile, an abandoned request whose usage was never seen is also `usage_missing` (decision 12): a non-streamed response is generated whole before it is sent, so hanging up cannot un-spend it, and the record decides `capped`, with the pair parked and paged rather than left to spend unmeasured.
11. **A passes profile clamps `max_tokens` to 64,000** (`MAX_TOKENS_CEILING`), the spec's "caps ... `max_tokens`" for a provider that cannot report usage. A tokens profile clamps to whatever its pair has left after in-flight reservations.
12. **A response without usage on a tokens profile** is passed through to the worker. The pair is marked `usage_missing`, reported, and paged (`PAGE:` on stdout, overview decision 14), and every later request on that token is refused. The control plane commits that record as `capped` (overview decision 2).
13. **The token travels as `Authorization: Bearer`**, which is how Claude Code sends both `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`. `x-api-key` is accepted too. Neither is ever forwarded upstream.
14. **A flush waits at most 50 seconds and answers `{last_seq, drained}`.** It is drained only when the token has no request in flight as well as nothing queued: a request still streaming has no sequence number yet, and its report can be the one that decides the pair (a hang-up the gateway has not yet seen, on its next write). A pair's worker sends nothing more once its pass is over, so the wait covers requests already admitted, and one that outlasts the budget is the incomplete flush the spec answers with `deferred`. `last_seq` is the last sequence number the gateway emitted for the token, not the last one acknowledged, since the acknowledged one is the control plane's own commit and could never reveal a gap. The control plane's gateway client must therefore time out after more than 50 seconds.
15. **The listener is bounded, overall and per peer.** A connection idle for 60 seconds (`IDLE_SECONDS`) is closed. Past 256 open connections (`MAX_CONNECTIONS`), or past 16 from one peer address (`MAX_CONNECTIONS_PER_PEER`, twice `IN_FLIGHT_MAX`), a new one is closed at once. Both are charged at accept, before a byte is read, so a client that never sends a token is bounded too. Each worker is its own container with its own address, so one prompt-injected worker cannot take the listener from every other job.
16. **A clean stop drains.** On `SIGTERM` the gateway stops accepting requests and gives queued reports up to 10 seconds to reach the control plane, then logs whether they drained. A crash loses what was queued, within the spec's stated bound.
17. **`TOKEN_REPORTING` stays provisional** (Phase 1 decision 6). Narrowing it needs a live response from each provider type, which is a check the human runs by hand (below).
18. **A cleartext `base_url` is refused unless its host is loopback, and a `base_url` whose port is not a number is refused at load.** The gateway sends the profile's credential there, and `workersai-shim.py` applies the same rule to its upstream.

## Review Focus

- **A usage field or a CRLF frame terminator split across two reads of the stream.** Expected: counted. Pinned in Task 3, at every split point of an LF and a CRLF sample.
- **A prompt-injected worker firing many requests at once with its token.** Expected: no more than the request cap reaches upstream, the granted `max_tokens` stays within the token cap, no more than `IN_FLIGHT_MAX` are in flight, and a reservation shortfall is paced, never reported as a cap hit. Pinned in Task 4.
- **A worker that hangs up, or a forward that raises.** Expected: the reservation is released, the pass is never `ok`, and unmeasured spend on a tokens profile fails the pair closed. Pinned in Task 4.
- **The provider cuts a response off mid-stream.** Expected: a failure, never a success. Pinned in Task 4.
- **A deployment change while jobs run.** Expected: a running token keeps its dispatch-time profile. Pinned in Task 4.
- **The gateway restarts mid-job, or gets a stale `hello`.** Expected: nothing is served until the `hello`, the caps resume from the committed counts, and a stale `hello` never moves a count back. Pinned in Task 4.
- **The control plane is down while a pass runs.** Expected: the pass is served within its caps, the reports buffer, and a flush says it did not drain. Pinned in Task 4.

## Not run here

- **Anthropic accepting a forwarded OAuth request.** Spike S3 established the header shape offline. Whether Anthropic accepts the result needs a real subscription token. The human runs it by hand after 2a.5, when a deployment exists.
- **Which providers report usage.** `ollama`'s Anthropic-compatible endpoint, Anthropic itself, and a `custom` upstream are each checked live by hand. The result narrows `deploy_config.TOKEN_REPORTING` in a later commit, which the spec calls "established in phase 2a from live responses".
- **The 60-second idle timeout under a real stall.** The test pins the configured value, not a minute-long wait.

---

### Task 1: The deployment file's endpoint keys

**Files:**
- Modify: `reviewer/deploy_config.py` (`KEYS`, `AUTH_MODES`, `Profile`, `_base_url_problem`, `_profile`)
- Modify: `tests/test_deploy_config.py` (a `custom()` fixture; the parity rows; a new `EndpointKeysTest`)

**Interfaces:**
- Consumes: `providers.check_url` (unchanged).
- Produces:
  - `deploy_config.AUTH_MODES: Dict[str, Tuple[Optional[str], Tuple[str, ...]]]`: `anthropic` gives `("api_key", ("api_key", "oauth_token"))` and `custom` gives `(None, ("bearer", "x_api_key"))`.
  - `Profile` gains `base_url: Optional[str] = None` and `auth: Optional[str] = None`, as its last two fields. An `anthropic` profile with no `auth` loads as `api_key`, and `ollama`, `cloudflare` and `workersai` load with `None` for both.
  - Problem texts: `is required for a custom profile`; `applies only to a custom profile`; `must be https unless its host is loopback; the gateway sends the credential there`; `is required for a custom profile: bearer or x_api_key`; `must be api_key or oauth_token; got 'bearer'`; `does not apply to a <provider> profile`; and `check_url`'s own two messages, minus their leading name.

- [ ] **Step 1: Write the failing tests**

Apply this change to `tests/test_deploy_config.py`. The existing custom-profile fixtures gain the keys that are now required. The parity test gains rows for the two new leaves. A new class pins the rules:

```diff
diff --git a/tests/test_deploy_config.py b/tests/test_deploy_config.py
index 8283e41..0bf2763 100644
--- a/tests/test_deploy_config.py
+++ b/tests/test_deploy_config.py
@@ -24,6 +24,13 @@ def profile(**over):
     return p
 
 
+def custom(**over):
+    """A custom profile: phase 2a.2 makes base_url and auth required on one."""
+    fields = {"provider": "custom", "base_url": "https://llm.example", "auth": "bearer"}
+    fields.update(over)
+    return profile(**fields)
+
+
 def load(doc):
     return deploy_config.load_deploy(json.dumps(doc).encode("utf-8"))
 
@@ -151,8 +158,8 @@ class LoadTest(unittest.TestCase):
         # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 656.
         # Mutation: Permit custom token budgets without usage reported.
         self.assertIn("profiles.c.budget.unit",
-                      problems({"profiles": {"c": profile(provider="custom")}}))
-        d = load({"profiles": {"c": profile(provider="custom", usage="reported")}})
+                      problems({"profiles": {"c": custom()}}))
+        d = load({"profiles": {"c": custom(usage="reported")}})
         self.assertTrue(d.profiles["c"].usage_reported)
 
     def test_usage_is_only_for_custom(self):
@@ -376,6 +383,10 @@ class DeployLeafCoverageTest(unittest.TestCase):
             ("profiles.*.concurrency", "concurrency", 2, 3, 2, 3),
             # None here means omitted: usage has only one accepted explicit value.
             ("profiles.*.usage", "usage_reported", None, "reported", False, True),
+            ("profiles.*.base_url", "base_url",
+             "https://a.example", "https://b.example", "https://a.example", "https://b.example"),
+            # None here means omitted: anthropic's default is api_key.
+            ("profiles.*.auth", "auth", None, "oauth_token", "api_key", "oauth_token"),
             ("profiles.*.limit_backoff_seconds", "limit_backoff_seconds", 17, 19, 17, 19),
             ("profiles.*.budget.unit", "budget_unit", "tokens", "passes", "tokens", "passes"),
             ("profiles.*.budget.daily", "budget_daily", 101, 103, 101, 103),
@@ -406,8 +417,11 @@ class DeployLeafCoverageTest(unittest.TestCase):
                 for value in (before, after):
                     doc = {"profiles": {"o": profile()}}
                     if path == "profiles.*.usage":
-                        doc["profiles"]["o"] = profile(
-                            provider="custom", budget={"unit": "passes", "daily": 101})
+                        doc["profiles"]["o"] = custom(budget={"unit": "passes", "daily": 101})
+                    if path == "profiles.*.base_url":
+                        doc["profiles"]["o"] = custom(usage="reported")
+                    if path == "profiles.*.auth":
+                        doc["profiles"]["o"] = profile(provider="anthropic")
                     concrete = path.replace("profiles.*.", "profiles.o.")
                     concrete = concrete.replace("overrides.*", "overrides.api")
                     keys = concrete.split(".")
@@ -421,3 +435,67 @@ class DeployLeafCoverageTest(unittest.TestCase):
                     observed.append(getattr(target, field))
                 self.assertEqual(observed, [expected_before, expected_after])
                 self.assertNotEqual(observed[0], observed[1], "declared leaf has no observable effect")
+
+
+class EndpointKeysTest(unittest.TestCase):
+    """Phase 2a.2: what the gateway needs to reach a custom upstream and present a credential."""
+
+    def test_a_custom_profile_needs_base_url_and_auth(self):
+        text = problems({"profiles": {"c": profile(provider="custom", usage="reported")}})
+        self.assertIn("profiles.c.base_url: is required for a custom profile", text)
+        self.assertIn("profiles.c.auth: is required for a custom profile: bearer or x_api_key", text)
+
+    def test_a_custom_profile_loads_with_both(self):
+        p = load({"profiles": {"c": custom(auth="x_api_key", usage="reported")}}).profiles["c"]
+        self.assertEqual((p.base_url, p.auth), ("https://llm.example", "x_api_key"))
+
+    def test_base_url_is_refused_on_any_other_provider(self):
+        for provider in ("ollama", "anthropic", "cloudflare", "workersai"):
+            with self.subTest(provider=provider):
+                self.assertIn("profiles.o.base_url: applies only to a custom profile",
+                              problems({"profiles": {"o": profile(
+                                  provider=provider, base_url="https://x.example")}}))
+
+    def test_a_base_url_ending_in_an_endpoint_path_is_refused(self):
+        self.assertIn("ends with an endpoint path",
+                      problems({"profiles": {"c": custom(base_url="https://llm.example/v1/messages",
+                                                         usage="reported")}}))
+
+    def test_a_port_that_is_not_a_number_is_refused(self):
+        for url in ("http://127.0.0.1:notaport", "https://llm.example:99999"):
+            with self.subTest(url=url):
+                self.assertIn("has a port that is not a number",
+                              problems({"profiles": {"c": custom(base_url=url, usage="reported")}}))
+
+    def test_cleartext_is_refused_except_to_loopback(self):
+        self.assertIn("must be https unless its host is loopback",
+                      problems({"profiles": {"c": custom(base_url="http://llm.example",
+                                                         usage="reported")}}))
+        for host in ("127.0.0.1:8080", "localhost", "[::1]:9"):
+            with self.subTest(host=host):
+                load({"profiles": {"c": custom(base_url=f"http://{host}", usage="reported")}})
+
+    def test_a_non_url_is_refused(self):
+        self.assertIn("must be an http(s) URL",
+                      problems({"profiles": {"c": custom(base_url="llm.example", usage="reported")}}))
+
+    def test_anthropic_auth_defaults_to_api_key_and_accepts_oauth_token(self):
+        self.assertEqual(load({"profiles": {"a": profile(provider="anthropic")}})
+                         .profiles["a"].auth, "api_key")
+        self.assertEqual(load({"profiles": {"a": profile(provider="anthropic", auth="oauth_token")}})
+                         .profiles["a"].auth, "oauth_token")
+
+    def test_an_auth_mode_from_another_provider_is_refused(self):
+        self.assertIn("profiles.a.auth: must be api_key or oauth_token; got 'bearer'",
+                      problems({"profiles": {"a": profile(provider="anthropic", auth="bearer")}}))
+
+    def test_auth_is_refused_where_the_provider_has_no_modes(self):
+        for provider in ("ollama", "cloudflare", "workersai"):
+            with self.subTest(provider=provider):
+                self.assertIn(f"profiles.o.auth: does not apply to a {provider} profile",
+                              problems({"profiles": {"o": profile(provider=provider, auth="bearer")}}))
+
+    def test_an_ollama_profile_still_needs_neither(self):
+        p = load({"profiles": {"o": profile()}}).profiles["o"]
+        self.assertEqual((p.base_url, p.auth), (None, None))
+
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_deploy_config.py'`
Expected: FAIL. `EndpointKeysTest` fails, and the parity test reports two leaves with no `KEYS` entry.

- [ ] **Step 3: Implement**

Apply this change to `reviewer/deploy_config.py`:

```diff
diff --git a/reviewer/deploy_config.py b/reviewer/deploy_config.py
index 14c5193..eb0b482 100644
--- a/reviewer/deploy_config.py
+++ b/reviewer/deploy_config.py
@@ -9,12 +9,13 @@ YAML, by founder ruling (2026-10-09).
 """
 
 import re
+import urllib.parse
 from dataclasses import dataclass
 from typing import Dict, List, Optional, Tuple, Union
 
 from common import ConfigError
 from config_load import Problems, is_int, is_obj, is_str, join, read_json
-from providers import PROVIDERS
+from providers import PROVIDERS, check_url
 
 DEPLOY_FILE = "claudebox.deploy.json"
 DEPLOY_FILE_CAP = 256 * 1024
@@ -28,6 +29,7 @@ KEYS = {
     "profiles": {"*": {
         "provider": None, "models": None, "default_model": None, "credential": None,
         "concurrency": None, "usage": None, "limit_backoff_seconds": None,
+        "base_url": None, "auth": None,
         "budget": {"unit": None, "daily": None, "repo_share": None},
         "per_pair": {"requests": None, "tokens": None},
     }},
@@ -43,6 +45,16 @@ KEYS = {
 _NAME_OK = re.compile(r"[A-Za-z0-9._-]+")
 _ENV_NAME_OK = re.compile(r"[A-Z_][A-Z0-9_]*")
 
+# How the gateway presents a profile's credential upstream, per provider type
+# (phase 2a.2). None means the key is refused for that type. The first value
+# is the default; a type with no default must state it. A hosted profile has
+# no environment to infer this from, and guessing from a credential's prefix
+# is the inference the design refuses.
+AUTH_MODES: Dict[str, Tuple[Optional[str], Tuple[str, ...]]] = {
+    "anthropic": ("api_key", ("api_key", "oauth_token")),
+    "custom": (None, ("bearer", "x_api_key")),
+}
+
 
 class DeployError(ConfigError):
     """Every schema problem in the deployment file, one per line."""
@@ -67,6 +79,8 @@ class Profile:
     per_pair_requests: int
     per_pair_tokens: Optional[int]
     usage_reported: bool
+    base_url: Optional[str] = None
+    auth: Optional[str] = None
 
 
 @dataclass(frozen=True)
@@ -116,6 +130,31 @@ def _int(problems, raw, key, path, default, minimum=0) -> int:
     return default
 
 
+def _base_url_problem(value) -> Optional[str]:
+    """Why a custom profile's base_url is unusable, or None.
+
+    providers.check_url's two rules (an http(s) URL, no endpoint path), plus
+    one of the gateway's own: it relays the profile's credential there, so
+    cleartext is refused unless the host is loopback, the same rule
+    workersai-shim.py applies to its upstream.
+    """
+    if not is_str(value) or not value:
+        return "is required for a custom profile"
+    try:
+        check_url("base_url", value)
+    except ConfigError as exc:
+        return str(exc)[len("base_url "):]
+    try:
+        urllib.parse.urlsplit(value).port
+    except ValueError:
+        return "has a port that is not a number from 0 to 65535"
+    if value.startswith("http://"):
+        host = urllib.parse.urlsplit(value).hostname or ""
+        if host not in ("127.0.0.1", "localhost", "::1"):
+            return "must be https unless its host is loopback; the gateway sends the credential there"
+    return None
+
+
 def _profile(problems: Problems, name: str, raw) -> Optional[Profile]:
     path = join("profiles", name)
     before = len(problems.lines())
@@ -149,6 +188,30 @@ def _profile(problems: Problems, name: str, raw) -> Optional[Profile]:
         problems.add(join(path, "usage"), "only 'reported', and only on a custom profile")
     usage_reported = usage == "reported" and provider == "custom"
 
+    base_url = raw.get("base_url")
+    if provider == "custom":
+        problem = _base_url_problem(base_url)
+        if problem:
+            problems.add(join(path, "base_url"), problem)
+            base_url = None
+    elif base_url is not None:
+        problems.add(join(path, "base_url"), "applies only to a custom profile")
+        base_url = None
+
+    auth = raw.get("auth")
+    default_auth, auth_modes = AUTH_MODES.get(provider, (None, ()))
+    if auth is None:
+        auth = default_auth
+        if provider in AUTH_MODES and auth is None:
+            problems.add(join(path, "auth"), f"is required for a {provider} profile: "
+                         f"{' or '.join(auth_modes)}")
+    elif provider in PROVIDERS and not auth_modes:
+        problems.add(join(path, "auth"), f"does not apply to a {provider} profile")
+        auth = None
+    elif auth_modes and auth not in auth_modes:
+        problems.add(join(path, "auth"), f"must be {' or '.join(auth_modes)}; got {auth!r}")
+        auth = None
+
     budget = _section(problems, raw, "budget", path)
     bpath = join(path, "budget")
     unit = budget.get("unit")
@@ -187,7 +250,8 @@ def _profile(problems: Problems, name: str, raw) -> Optional[Profile]:
     if len(problems.lines()) > before:
         return None
     return Profile(name, provider, tuple(models), default_model, credential, concurrency,
-                   unit, daily, float(repo_share), backoff, requests, tokens, usage_reported)
+                   unit, daily, float(repo_share), backoff, requests, tokens, usage_reported,
+                   base_url, auth)
 
 
 def load_deploy(data: bytes) -> Deploy:
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_deploy_config.py'`, then `./test-python.sh`
Expected: PASS, 50 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add reviewer/deploy_config.py tests/test_deploy_config.py
git commit -m "deploy: base_url and auth, what a hosted profile needs to reach its provider"
```

### Task 2: The socket protocol

**Files:**
- Create: `hosted/gwproto.py`
- Test: `tests/test_hosted_gwproto.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `gwproto.MAX_LINE = 4 * 1024 * 1024`, `gwproto.ProtocolError(Exception)`.
  - `encode(secret, op, fields) -> bytes` (one line) and `decode(line, secret) -> dict` (the message without its `secret`; `ProtocolError` on bad JSON, a non-object, a wrong or missing secret, or an oversized line).
  - `Server(path, secret, handler, mode=0o660)` with `start() -> Server` and `stop()`. `handler(msg) -> dict` is called once per message. A handler exception is answered as `{"error": "<Type>: <message>"}`, and a wrong secret closes the connection without a reply.
  - `Client(path, secret, timeout=10.0)` with `call(op, **fields) -> dict` (raises `OSError` or `ProtocolError`, and reconnects on the next call) and `close()`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_gwproto.py`:

```python
import _path  # noqa: F401

import os
import shutil
import socket
import tempfile
import unittest

from gwproto import MAX_LINE, Client, ProtocolError, Server, decode, encode


class CodecTest(unittest.TestCase):
    def test_a_message_round_trips_without_its_secret(self):
        line = encode("s3cret", "flush", {"credential_id": "abc"})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(decode(line, "s3cret"), {"op": "flush", "credential_id": "abc"})

    def test_a_wrong_or_missing_secret_is_refused(self):
        with self.assertRaises(ProtocolError):
            decode(encode("other", "flush", {}), "s3cret")
        with self.assertRaises(ProtocolError):
            decode(b'{"op": "flush"}\n', "s3cret")

    def test_junk_is_refused(self):
        for junk in (b"not json\n", b"[1]\n", b'"x"\n'):
            with self.subTest(junk=junk), self.assertRaises(ProtocolError):
                decode(junk, "s")

    def test_an_oversized_line_is_refused(self):
        with self.assertRaises(ProtocolError):
            decode(b"x" * (MAX_LINE + 1), "s")


class SocketTest(unittest.TestCase):
    def setUp(self):
        # AF_UNIX paths are short on macOS; mkdtemp under /tmp keeps them so.
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "g.sock")
        self.seen = []

        def handler(msg):
            self.seen.append(msg)
            if msg.get("op") == "boom":
                raise RuntimeError("handler bug")
            return {"echo": msg.get("value")}

        self.server = Server(self.path, "s3cret", handler).start()
        self.addCleanup(self.server.stop)

    def test_a_call_gets_its_reply(self):
        client = Client(self.path, "s3cret")
        self.assertEqual(client.call("ping", value=1), {"echo": 1})
        self.assertEqual(client.call("ping", value=2), {"echo": 2})
        client.close()

    def test_the_socket_file_is_0660(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o660)

    def test_a_client_with_the_wrong_secret_gets_no_reply_and_reaches_no_handler(self):
        client = Client(self.path, "wrong", timeout=2)
        with self.assertRaises(ProtocolError):
            client.call("ping", value=1)
        self.assertEqual(self.seen, [])

    def test_a_reply_carrying_the_wrong_secret_is_refused_by_the_client(self):
        other = os.path.join(self.dir, "o.sock")
        impostor = Server(other, "impostor", lambda m: {"echo": 1}).start()
        self.addCleanup(impostor.stop)
        # The impostor drops our message for its wrong secret, so the client sees
        # a closed connection, never a forged answer.
        with self.assertRaises(ProtocolError):
            Client(other, "s3cret", timeout=2).call("ping")

    def test_a_handler_exception_is_answered_and_the_server_lives(self):
        client = Client(self.path, "s3cret")
        reply = client.call("boom")
        self.assertIn("RuntimeError: handler bug", reply["error"])
        self.assertEqual(client.call("ping", value=3), {"echo": 3})
        client.close()

    def test_a_client_reconnects_after_the_connection_drops(self):
        client = Client(self.path, "s3cret")
        client.call("ping", value=1)
        client._conn.shutdown(socket.SHUT_RDWR)
        with self.assertRaises((ProtocolError, OSError)):
            client.call("ping", value=2)
        self.assertEqual(client.call("ping", value=3), {"echo": 3})
        client.close()

    def test_no_server_is_an_oserror(self):
        with self.assertRaises(OSError):
            Client(os.path.join(self.dir, "none.sock"), "s3cret").call("ping")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_gwproto.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'gwproto'`.

- [ ] **Step 3: Implement**

`hosted/gwproto.py`:

```python
"""Line-delimited JSON over a unix socket, between processes in one container.

The control plane and the gateway talk this way (and, from 2a.3, the control
plane and runnerd). Each message is one JSON object on one line, and every
message in both directions carries the per-deployment secret the supervisor
hands to both processes at startup. A message whose secret does not match
closes the connection without a reply: a process that does not hold the
secret learns nothing from the socket, not even which ops exist.

The socket file is created mode 0660, so only the owner and the group the
supervisor puts the two uids in can connect at all; the secret is the second
layer, for the case where a third process lands in that group.

One request, one reply, in order, on a connection either side may hold open.
"""

import hmac
import json
import os
import socket
import threading
from typing import Any, Callable, Dict, Optional

# A hello carrying every live token of a busy deployment is the largest
# message; 4 MiB holds tens of thousands of rows.
MAX_LINE = 4 * 1024 * 1024


class ProtocolError(Exception):
    """A message that is not ours: bad JSON, a wrong secret, or too long."""


def encode(secret: str, op: Optional[str], fields: Dict[str, Any]) -> bytes:
    msg = dict(fields)
    msg["secret"] = secret
    if op is not None:
        msg["op"] = op
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes, secret: str) -> Dict[str, Any]:
    """The message without its secret. Raises ProtocolError on anything else."""
    if len(line) > MAX_LINE:
        raise ProtocolError("message too long")
    try:
        msg = json.loads(line)
    except ValueError:
        raise ProtocolError("not JSON") from None
    if not isinstance(msg, dict):
        raise ProtocolError("not a JSON object")
    presented = msg.pop("secret", None)
    if not isinstance(presented, str) or not hmac.compare_digest(
            presented.encode("utf-8"), secret.encode("utf-8")):
        raise ProtocolError("wrong secret")
    return msg


def _readline(f) -> bytes:
    line = f.readline(MAX_LINE + 1)
    if len(line) > MAX_LINE:
        raise ProtocolError("message too long")
    return line


Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


class Server:
    """Accepts connections on a unix socket and answers each message with handler(msg)."""

    def __init__(self, path: str, secret: str, handler: Handler, mode: int = 0o660):
        self.path = path
        self.secret = secret
        self.handler = handler
        if os.path.exists(path):
            os.unlink(path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, mode)
        self._sock.listen(16)
        self._stopping = False
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def start(self) -> "Server":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _accept(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as rf, conn.makefile("wb") as wf:
            while True:
                try:
                    line = _readline(rf)
                    if not line:
                        return
                    msg = decode(line, self.secret)
                except (ProtocolError, OSError):
                    return
                try:
                    reply = self.handler(msg)
                except Exception as exc:  # a handler bug answers, never kills the server
                    reply = {"error": f"{type(exc).__name__}: {exc}"}
                try:
                    wf.write(encode(self.secret, None, reply))
                    wf.flush()
                except OSError:
                    return


class Client:
    """One persistent connection; reconnects on the next call after a failure."""

    def __init__(self, path: str, secret: str, timeout: float = 10.0):
        self.path = path
        self.secret = secret
        self.timeout = timeout
        self._lock = threading.Lock()
        self._conn: Optional[socket.socket] = None
        self._rf = self._wf = None

    def _connect(self) -> None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.timeout)
        conn.connect(self.path)
        self._conn, self._rf, self._wf = conn, conn.makefile("rb"), conn.makefile("wb")

    def close(self) -> None:
        for f in (self._rf, self._wf, self._conn):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        self._conn = self._rf = self._wf = None

    def call(self, op: str, **fields: Any) -> Dict[str, Any]:
        """Send one message and return the reply. Raises OSError or ProtocolError."""
        with self._lock:
            try:
                if self._conn is None:
                    self._connect()
                self._wf.write(encode(self.secret, op, fields))
                self._wf.flush()
                line = _readline(self._rf)
                if not line:
                    raise ProtocolError("connection closed without a reply")
                return decode(line, self.secret)
            except BaseException:
                self.close()
                raise
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_gwproto.py'`, then `./test-python.sh`
Expected: PASS, 11 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/gwproto.py tests/test_hosted_gwproto.py
git commit -m "hosted: the socket protocol between the control plane and the gateway"
```

### Task 3: The route's pure parts: profiles, headers, usage, classification

**Files:**
- Create: `hosted/gateway.py` (its first half: everything above `TokenState`)
- Test: `tests/test_hosted_gateway.py` (its first half)

**Interfaces:**
- Consumes: nothing beyond the standard library. The imports of `creds` and `gwproto` are used by Task 4.
- Produces:
  - Constants: `SERVED_PROVIDERS = ("ollama", "anthropic", "custom")`, `UPSTREAMS`, `BODY_MAX = 4 * 1024 * 1024`, `ROUTES`, `MAX_TOKENS_CEILING = 64000`, `JSON_KEEP`, `LIMIT_TYPES`, `FLUSH_SECONDS = 50.0`.
  - `unserved(name, provider) -> str` (`profiles.<name>: <provider> profiles are not served by the hosted gateway until phase 2a.6`) and `served_problems(profiles) -> List[str]` (one such line per unserved profile, sorted by name).
  - `Route(name, provider, base_url, auth, credential, per_pair_requests, per_pair_tokens, tokens_unit)`, frozen, with `Route.from_profile(p, env, upstreams) -> Route`. It raises `ValueError` with `unserved`'s words for an unserved provider, and naming the variable for a credential missing from `env`. `auth` resolves to `"bearer"` or `"x_api_key"`, and `credential` holds the secret's name, never its value.
  - `upstream_headers(route, value, incoming, length) -> Dict[str, str]`: the pass-through set plus the injected credential `value` and `content-length`, with names lowercased.
  - `UsageMeter(streaming)` with `feed(chunk)`, `finish()`, and the attributes `input_tokens`, `output_tokens`, `saw_usage`, `saw_stop` and `error_type`.
  - `classify(path, status, meter) -> str`, one of `success`, `limit`, `failure`, `rejected` and `neutral`.
  - Constants also include `CREDENTIAL_STATUSES = (401, 403)`, `IN_FLIGHT_MAX = 8`, `DRAIN_SECONDS = 10.0`, `IDLE_SECONDS = 60`, `MAX_CONNECTIONS = 256` and `MAX_CONNECTIONS_PER_PEER = 16`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_gateway.py`, the meter, classification and route cases (Task 4 adds the rest of the file):

```python
import _path  # noqa: F401

import http.client
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import creds
import gateway
import gwproto
from records import Records

SSE_OK = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":100,"cache_read_input_tokens":20,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":30}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
SSE_NO_USAGE = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
SSE_NO_STOP = SSE_OK[:SSE_OK.index(b"event: message_stop")]
SSE_OVERLOADED = (b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error",'
                  b'"message":"busy"}}\n\n')


def route_profile(**over):
    p = {"name": "p", "provider": "anthropic", "auth": "api_key", "base_url": None,
         "credential": "ANTHROPIC_API_KEY", "per_pair_requests": 200,
         "per_pair_tokens": 4000000, "budget_unit": "tokens"}
    p.update(over)
    return p


class MeterTest(unittest.TestCase):
    def feed_split(self, sample, cut):
        meter = gateway.UsageMeter(streaming=True)
        meter.feed(sample[:cut])
        meter.feed(sample[cut:])
        meter.finish()
        return meter

    def test_streamed_usage_is_counted_across_any_split(self):
        for cut in range(1, len(SSE_OK)):
            m = self.feed_split(SSE_OK, cut)
            self.assertEqual((m.input_tokens, m.output_tokens, m.saw_usage, m.saw_stop),
                             (120, 30, True, True), f"split at {cut}")

    def test_crlf_framing_is_read_across_any_split(self):
        crlf = SSE_OK.replace(b"\n", b"\r\n")
        for cut in range(1, len(crlf)):
            m = self.feed_split(crlf, cut)
            self.assertEqual((m.output_tokens, m.saw_stop), (30, True), f"split at {cut}")

    def test_a_stream_error_event_is_recorded(self):
        meter = gateway.UsageMeter(streaming=True)
        meter.feed(SSE_OVERLOADED)
        self.assertEqual(meter.error_type, "overloaded_error")

    def test_a_json_response_is_read_at_the_end(self):
        meter = gateway.UsageMeter(streaming=False)
        meter.feed(b'{"type":"message","usage":{"input_tokens":5,')
        meter.feed(b'"output_tokens":7}}')
        meter.finish()
        self.assertEqual((meter.input_tokens, meter.output_tokens, meter.saw_stop), (5, 7, True))

    def test_no_usage_and_no_stop_are_seen_as_absent(self):
        meter = gateway.UsageMeter(streaming=True)
        meter.feed(SSE_NO_STOP[:40])
        self.assertFalse(meter.saw_usage)
        self.assertFalse(meter.saw_stop)


class ClassifyTest(unittest.TestCase):
    def meter(self, error=None, stop=True, streaming=True):
        m = gateway.UsageMeter(streaming=streaming)
        m.error_type, m.saw_stop = error, stop
        return m

    def test_the_outcomes(self):
        cases = [
            ("/v1/messages", 200, None, True, "success"),
            ("/v1/messages/count_tokens", 200, None, True, "neutral"),
            ("/v1/messages", 200, None, False, "failure"),
            ("/v1/messages", 429, None, True, "limit"),
            ("/v1/messages", 529, None, True, "limit"),
            ("/v1/messages", 200, "overloaded_error", True, "limit"),
            ("/v1/messages", 200, "rate_limit_error", True, "limit"),
            ("/v1/messages", 200, "api_error", True, "failure"),
            ("/v1/messages", 500, None, True, "failure"),
            ("/v1/messages", 401, None, True, "failure"),
            ("/v1/messages", 403, None, True, "failure"),
            ("/v1/messages", 400, None, True, "rejected"),
            ("/v1/messages", 404, None, True, "rejected"),
            ("/v1/messages", 422, None, True, "rejected"),
        ]
        for path, status, error, stop, expected in cases:
            with self.subTest(status=status, error=error, stop=stop):
                self.assertEqual(gateway.classify(path, status, self.meter(error, stop)), expected)

    def test_a_json_success_needs_no_stop_event(self):
        self.assertEqual(gateway.classify("/v1/messages", 200,
                                          self.meter(stop=False, streaming=False)), "success")


class RouteTest(unittest.TestCase):
    def headers(self, route, value):
        incoming = {"Authorization": "Bearer cbx_p_worker", "x-api-key": "worker",
                    "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json", "Cookie": "c=1"}
        return gateway.upstream_headers(route, value, incoming, 12)

    def test_an_anthropic_api_key_goes_in_x_api_key_and_the_worker_token_does_not(self):
        r = gateway.Route.from_profile(route_profile(), {"ANTHROPIC_API_KEY": "sk-real"},
                                       gateway.UPSTREAMS)
        h = self.headers(r, "sk-real")
        self.assertEqual((r.base_url, r.credential), ("https://api.anthropic.com", "ANTHROPIC_API_KEY"))
        self.assertEqual(h["x-api-key"], "sk-real")
        self.assertNotIn("authorization", h)
        self.assertNotIn("cookie", h)
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(h["content-length"], "12")

    def test_an_oauth_token_replaces_only_the_bearer(self):
        r = gateway.Route.from_profile(route_profile(auth="oauth_token",
                                                     credential="CLAUDE_CODE_OAUTH_TOKEN"),
                                       {"CLAUDE_CODE_OAUTH_TOKEN": "oat-real"}, gateway.UPSTREAMS)
        h = self.headers(r, "oat-real")
        self.assertEqual(h["authorization"], "Bearer oat-real")
        self.assertNotIn("x-api-key", h)
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")

    def test_ollama_is_a_bearer_to_ollama_com(self):
        r = gateway.Route.from_profile(route_profile(provider="ollama", auth=None,
                                                     credential="OLLAMA_API_KEY"),
                                       {"OLLAMA_API_KEY": "olk"}, gateway.UPSTREAMS)
        self.assertEqual((r.base_url, self.headers(r, "olk")["authorization"]),
                         ("https://ollama.com", "Bearer olk"))

    def test_custom_uses_its_base_url_and_auth_mode(self):
        r = gateway.Route.from_profile(route_profile(provider="custom", auth="x_api_key",
                                                     base_url="https://llm.example/",
                                                     credential="K"), {"K": "v"},
                                       gateway.UPSTREAMS)
        self.assertEqual((r.base_url, self.headers(r, "v")["x-api-key"]), ("https://llm.example", "v"))

    def test_an_unserved_provider_is_refused_in_served_problems_words(self):
        with self.assertRaises(ValueError) as ctx:
            gateway.Route.from_profile(route_profile(name="w", provider="workersai"),
                                       {"ANTHROPIC_API_KEY": "x"}, gateway.UPSTREAMS)
        self.assertEqual(str(ctx.exception), "profiles.w: workersai profiles are not served "
                                             "by the hosted gateway until phase 2a.6")

    def test_a_credential_missing_from_the_environment_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            gateway.Route.from_profile(route_profile(), {}, gateway.UPSTREAMS)
        self.assertIn("ANTHROPIC_API_KEY is not in the gateway's environment", str(ctx.exception))

    def test_served_problems_names_each_unserved_profile(self):
        profiles = {"a": SimpleNamespace(provider="anthropic"),
                    "w": SimpleNamespace(provider="workersai"),
                    "c": SimpleNamespace(provider="cloudflare")}
        self.assertEqual(gateway.served_problems(profiles), [
            "profiles.c: cloudflare profiles are not served by the hosted gateway until phase 2a.6",
            "profiles.w: workersai profiles are not served by the hosted gateway until phase 2a.6"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_gateway.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'gateway'`.

- [ ] **Step 3: Implement**

`hosted/gateway.py`, its first half:

```python
"""The gateway's provider route: the worker's only way to a model provider.

A worker's `claude` points ANTHROPIC_BASE_URL here and presents a per-pair
provider token. The gateway checks the token, overwrites the model, enforces
the pair's caps, swaps the token for the profile's real credential, streams
the upstream response back unbuffered, and counts what it saw. It holds the
provider credentials; the control plane, which holds the App key, does not,
and no worker ever does.

It is the author of every pair's provider outcome: for each token it keeps a
record (requests, successes, tokens, the first usage limit, the first
failure, the first rejected request, a cap hit) and reports every change to
the control plane over a unix socket, numbered per token, so the control
plane knows when it holds all of them. It enforces caps from its own counts,
reserving each request before forwarding it, so provider traffic never waits
on the control plane and a client that is not Claude Code cannot outrun a cap.

Each token carries the profile it was dispatched under, so a deployment
change reaches new jobs and leaves running ones alone, and a token whose
profile cannot be served is refused on its own. Until the control plane's
hello has told it the live tokens and their committed counts, the gateway
serves no provider traffic. A crash loses the reports not yet acknowledged,
a window the per-pair caps bound; a clean stop drains them first.

It does not buffer, cache, log or retry a body.
"""

import http.client
import http.server
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

import creds
import gwproto

# Phase 2a.2 serves these three. cloudflare and workersai arrive in 2a.6.
SERVED_PROVIDERS = ("ollama", "anthropic", "custom")
UPSTREAMS = {"ollama": "https://ollama.com", "anthropic": "https://api.anthropic.com"}
# The spec's "provider request body | 4 MB | fixed | request refused".
BODY_MAX = 4 * 1024 * 1024
ROUTES = {"/v1/messages", "/v1/messages/count_tokens"}
# What a non-reporting (passes) profile clamps max_tokens to, since it cannot
# count tokens: the spec's "it caps requests, body size and max_tokens".
MAX_TOKENS_CEILING = 64000
# How much of a non-streamed response is kept to read its usage from. A
# messages response is a few KB; anything longer is streamed through uncounted.
JSON_KEEP = 4 * 1024 * 1024
LIMIT_TYPES = ("rate_limit_error", "overloaded_error")
# Upstream statuses that mean the deployment's credential was refused: the
# operator's problem, so a provider failure. Every other 4xx except a limit is
# the request's own fault, and the request is the worker's.
CREDENTIAL_STATUSES = (401, 403)
# Requests one token may have in flight at once. Admission reserves a request
# and its max_tokens under the lock, so this bounds what one token can hold
# reserved, and a client ignoring the 429 cannot pin more gateway threads.
IN_FLIGHT_MAX = 8
FLUSH_SECONDS = 50.0
DRAIN_SECONDS = 10.0
# The listener's own bounds. Its client is a worker, which may be
# prompt-injected, so neither an idle connection nor a flood of them may hold
# gateway threads without limit: a socket read or write that stalls for
# IDLE_SECONDS ends the connection, and past MAX_CONNECTIONS a new one is
# closed at once. Streaming is unaffected: a live stream is never idle.
IDLE_SECONDS = 60
MAX_CONNECTIONS = 256
# One peer's share of those. Each worker is its own container with its own
# address, so this holds one worker, with or without a token, to its own
# budget instead of letting it take every slot from every other job.
MAX_CONNECTIONS_PER_PEER = 2 * IN_FLIGHT_MAX
# Headers passed upstream from the worker's request, lowercased. Everything
# else, Authorization and x-api-key above all, is dropped.
_PASS_UP = ("content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent",
            "x-app", "anthropic-dangerous-direct-browser-access")
# Headers passed back from upstream to the worker.
_PASS_DOWN = ("content-type", "request-id", "retry-after", "x-should-retry")


def unserved(name: str, provider: str) -> str:
    return f"profiles.{name}: {provider} profiles are not served by the hosted gateway until phase 2a.6"


def served_problems(profiles: Mapping[str, Any]) -> List[str]:
    """One line per deployment profile this gateway cannot serve yet.

    `profiles` is deploy_config.Deploy.profiles. `hosted config apply` (2a.5)
    refuses a deployment file with any. The gateway itself refuses a token
    dispatched under such a profile with the same words.
    """
    return [unserved(name, p.provider) for name, p in sorted(profiles.items())
            if p.provider not in SERVED_PROVIDERS]


@dataclass(frozen=True)
class Route:
    """One profile, as the gateway needs it: where, and how to authenticate."""
    name: str
    provider: str
    base_url: str
    auth: str               # "bearer" or "x_api_key", resolved from the profile
    credential: str         # the secret's NAME; its value is read from the environment
    per_pair_requests: int
    per_pair_tokens: Optional[int]
    tokens_unit: bool

    @classmethod
    def from_profile(cls, p: Mapping[str, Any], env: Mapping[str, str],
                     upstreams: Mapping[str, str]) -> "Route":
        provider = p["provider"]
        if provider not in SERVED_PROVIDERS:
            raise ValueError(unserved(p["name"], provider))
        if not env.get(p["credential"], ""):
            raise ValueError(f"profiles.{p['name']}: {p['credential']} is not in the "
                             "gateway's environment")
        if provider == "custom":
            base, auth = p["base_url"], p["auth"]
        elif provider == "ollama":
            base, auth = upstreams["ollama"], "bearer"
        else:
            base = upstreams["anthropic"]
            auth = "bearer" if p.get("auth") == "oauth_token" else "x_api_key"
        return cls(p["name"], provider, base.rstrip("/"), auth, p["credential"],
                   int(p["per_pair_requests"]), p.get("per_pair_tokens"),
                   p.get("budget_unit") == "tokens")


def upstream_headers(route: Route, value: str, incoming: Mapping[str, str],
                     length: int) -> Dict[str, str]:
    """The headers sent upstream: a fixed pass-through set, plus the real credential."""
    headers = {k: v for k, v in ((k.lower(), v) for k, v in incoming.items()) if k in _PASS_UP}
    if route.auth == "bearer":
        headers["authorization"] = f"Bearer {value}"
    else:
        headers["x-api-key"] = value
    headers["content-length"] = str(length)
    return headers


class UsageMeter:
    """Reads usage, errors and completion out of a response as it streams past.

    Server-sent events are parsed incrementally, so a usage field or a frame
    terminator split across two reads is still seen. Input counts every kind
    of input token the provider reports (plain, cache creation, cache read);
    output takes the largest cumulative value seen, since message_delta
    repeats it.
    """

    def __init__(self, streaming: bool):
        self.streaming = streaming
        self.input_tokens = 0
        self.output_tokens = 0
        self.saw_usage = False
        self.saw_stop = False
        self.error_type: Optional[str] = None
        self._buf = b""
        self._kept = b""

    def _usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        inputs = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                         "cache_read_input_tokens")]
        if any(isinstance(v, int) for v in inputs):
            self.input_tokens = max(self.input_tokens, sum(v for v in inputs if isinstance(v, int)))
            self.saw_usage = True
        out = usage.get("output_tokens")
        if isinstance(out, int):
            self.output_tokens = max(self.output_tokens, out)
            self.saw_usage = True

    def _event(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "message_start":
            self._usage((data.get("message") or {}).get("usage"))
        elif kind == "message_delta":
            self._usage(data.get("usage"))
        elif kind == "message_stop":
            self.saw_stop = True
        elif kind == "error":
            self.error_type = (data.get("error") or {}).get("type") or "error"
        elif kind == "message":
            self._usage(data.get("usage"))
            self.saw_stop = True

    def feed(self, chunk: bytes) -> None:
        if not self.streaming:
            if len(self._kept) < JSON_KEEP:
                self._kept += chunk[:JSON_KEEP - len(self._kept)]
            return
        # Normalize the accumulated buffer, not the chunk: a CRLF can arrive
        # split across two reads.
        self._buf = (self._buf + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            data = b"\n".join(line[5:].lstrip() for line in raw.split(b"\n")
                              if line.startswith(b"data:"))
            if data:
                try:
                    self._event(json.loads(data))
                except ValueError:
                    pass

    def finish(self) -> None:
        if not self.streaming and self._kept:
            try:
                self._event(json.loads(self._kept))
            except ValueError:
                pass


def classify(path: str, status: int, meter: UsageMeter) -> str:
    """success | limit | failure | rejected | neutral. The record, as the gateway saw it.

    `rejected` is a 4xx the request itself caused (400, 404, 413, 422 and the
    like). The body is the worker's, so counting that as a provider failure
    would let a worker move the profile's failure counter, which parks the
    profile for every repo on it. A refused credential (401, 403) is the
    deployment's, and stays a failure. A 2xx stream that ends without its
    message_stop was truncated upstream, and is a failure.
    """
    if status in (429, 529) or meter.error_type in LIMIT_TYPES:
        return "limit"
    if 200 <= status < 300 and meter.error_type is None:
        if meter.streaming and not meter.saw_stop:
            return "failure"
        return "success" if path == "/v1/messages" else "neutral"
    if 400 <= status < 500 and status not in CREDENTIAL_STATUSES:
        return "rejected"
    return "failure"
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_gateway.py'`, then `./test-python.sh`
Expected: PASS, 14 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/gateway.py tests/test_hosted_gateway.py
git commit -m "hosted: the gateway route's profiles, headers and usage meter"
```

### Task 4: The gateway process: tokens, caps, forwarding, reports

**Files:**
- Modify: `hosted/gateway.py` (append its second half)
- Modify: `tests/test_hosted_gateway.py` (insert the process cases before its `if __name__` block)

**Interfaces:**
- Consumes: `creds.verify_against`, `creds.credential_id_of`, `creds.Creds` and `records.Records` (2a.1); `gwproto.Server` and `gwproto.Client` (Task 2); Task 3's names.
- Produces:
  - `TokenState` (a mutable dataclass) with `TokenState.from_row(row, env, upstreams)`. The row is a token row, which carries its own `route`, and its `counts` are the committed record. It raises `ValueError` when the route cannot be served.
  - `Reporter(call, retry_seconds=1.0)` with `start()`, `stop()`, `enqueue(report)`, `pending(credential_id=None) -> int`, `acked(credential_id) -> int` and `flush(credential_id_or_None, timeout=FLUSH_SECONDS) -> bool` (True when drained). `call` is a `gwproto.Client.call`.
  - `Gateway(env, reporter, upstreams=UPSTREAMS, clock=time.time, log=print, flush_seconds=FLUSH_SECONDS)` with `handle_control(msg) -> dict` (ops `hello` → `{ok, tokens, refused}`; `add_token`; `revoke`; `flush` → `{last_seq, drained}`; anything but `hello` before the first `hello` → `{error: "hello first", ready: false}`), `find(token) -> Optional[TokenState]`, `admit(state, path, requested_max) -> (refusal, max_tokens)`, where a refusal is `capped`, `requests`, `tokens` or `pacing`, `refuse_cap(state, cap)` and `complete(state, kind, granted, meter=None, detail="")`.
  - A report is `{credential_id, seq, delta: {requests, successes, input_tokens, output_tokens}}`, plus whichever of `first_limit`, `first_failure`, `first_rejected`, `first_abandoned`, `cap_hit` and `usage_missing` this response set for the first time.
  - `BoundedServer` (a `ThreadingHTTPServer` with `max_connections` and `max_per_peer`, both charged at accept), `make_server(gateway, host, port) -> BoundedServer` and `main(env=os.environ)`. `main` reads `GATEWAY_SECRET`, `GATEWAY_SOCKET`, `CONTROL_SOCKET` and `GATEWAY_LISTEN` (default `0.0.0.0:8090`). Its first stdout line is `gateway listening on <host>:<port>; waiting for the control plane's hello`, and on `SIGTERM` it drains reports and prints `gateway stopping; reports drained` (or `NOT drained`).

- [ ] **Step 1: Write the failing tests**

Insert these classes into `tests/test_hosted_gateway.py`, above its `if __name__` block:

```python
class Upstream(http.server.BaseHTTPRequestHandler):
    """A fake provider. Each test sets `reply` on the server: (status, content type, body).

    `delay` holds every response that long, so concurrency is observable, and
    `truncate` sends a chunked response and hangs up before its last chunk.
    """

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.server.lock:
            self.server.seen.append({"path": self.path, "headers": dict(self.headers),
                                     "body": json.loads(body)})
            self.server.in_flight += 1
            self.server.peak = max(self.server.peak, self.server.in_flight)
        time.sleep(self.server.delay)
        with self.server.lock:
            self.server.in_flight -= 1
        status, kind, payload = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("request-id", "req_1")
        if self.server.truncate:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.end_headers()
        self.wfile.write(payload)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir)
        # A fake provider on loopback, reached through a custom profile.
        self.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.up.seen, self.up.reply = [], (200, "text/event-stream", SSE_OK)
        self.up.lock, self.up.in_flight, self.up.peak = threading.Lock(), 0, 0
        self.up.delay, self.up.truncate = 0, False
        threading.Thread(target=self.up.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.up.server_close)
        self.addCleanup(self.up.shutdown)
        # A fake control plane that acknowledges every report.
        self.reports = []
        self.control_up = True

        def control(msg):
            if not self.control_up:
                raise RuntimeError("down")
            self.reports.append(msg)
            return {"ack": msg["seq"]}

        self.control = gwproto.Server(os.path.join(self.dir, "c.sock"), "s", control).start()
        self.addCleanup(self.control.stop)
        client = gwproto.Client(os.path.join(self.dir, "c.sock"), "s")
        self.reporter = gateway.Reporter(client.call, retry_seconds=0.05).start()
        self.addCleanup(self.reporter.stop)
        self.logs = []
        self.gw = gateway.Gateway({"K": "real-upstream-key"}, self.reporter, log=self.logs.append,
                                  flush_seconds=0.3)
        self.server = gateway.make_server(self.gw, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.records = Records(os.path.join(self.dir, "r.db"))
        self.creds = creds.Creds(self.records)

    def profile(self, **over):
        p = {"name": "p", "provider": "custom", "auth": "bearer",
             "base_url": f"http://127.0.0.1:{self.up.server_port}", "credential": "K",
             "per_pair_requests": 3, "per_pair_tokens": 1000, "budget_unit": "tokens"}
        p.update(over)
        return p

    def row(self, profile=None, counts=None, persona="sage"):
        token = self.creds.mint("provider", "job1", 600, persona=persona)
        cid = creds.credential_id_of(token)
        base = [r for r in self.creds.live_provider_rows() if r["credential_id"] == cid][0]
        return token, dict(base, model="the-model", route=profile or self.profile(),
                           counts=counts or {})

    def hello(self, profile=None, counts=None):
        token, row = self.row(profile, counts)
        reply = self.gw.handle_control({"op": "hello", "tokens": [row]})
        self.assertEqual(reply, {"ok": True, "tokens": 1, "refused": []})
        return token, row["credential_id"]

    def post(self, token, path="/v1/messages?beta=true", body=None, raw=None, length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        payload = raw if raw is not None else json.dumps(
            body if body is not None else {"model": "worker-chose", "max_tokens": 50,
                                           "messages": []}).encode()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "anthropic-version": "2023-06-01"}
        if length is not None:
            headers["Content-Length"] = str(length)
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def flush(self, cid):
        return self.gw.handle_control({"op": "flush", "credential_id": cid})

    # -- before and around the hello ----------------------------------------------
    def test_nothing_is_served_before_the_hello(self):
        token = self.creds.mint("provider", "job1", 600, persona="sage")
        status, body = self.post(token)
        self.assertEqual(status, 503)
        self.assertIn(b"starting", body)
        self.assertEqual(self.up.seen, [])

    def test_every_other_op_before_the_hello_asks_for_one(self):
        for msg in ({"op": "flush", "credential_id": "x"}, {"op": "revoke", "credential_ids": []},
                    {"op": "add_token", "token": {"credential_id": "x"}}):
            with self.subTest(op=msg["op"]):
                self.assertEqual(self.gw.handle_control(msg), {"error": "hello first", "ready": False})

    def test_a_second_hello_never_regresses_a_count_the_gateway_moved_past(self):
        token, cid = self.hello()
        self.post(token)
        self.flush(cid)
        stale = dict(self.row()[1], credential_id=cid, counts={"requests": 0, "seq": 0})
        stale.update({k: v for k, v in [r for r in self.creds.live_provider_rows()
                                         if r["credential_id"] == cid][0].items()})
        self.gw.handle_control({"op": "hello", "tokens": [stale]})
        self.assertEqual(self.gw.tokens[cid].requests, 1)
        self.assertEqual(self.gw.tokens[cid].seq, 1)

    def test_a_hello_drops_tokens_its_snapshot_leaves_out(self):
        token, cid = self.hello()
        self.gw.handle_control({"op": "hello", "tokens": []})
        self.assertEqual(self.post(token)[0], 401)

    def test_add_token_never_resets_a_token_already_held(self):
        token, cid = self.hello()
        self.post(token)
        _, again = self.row()
        again = dict(again, credential_id=cid, counts={})
        self.assertEqual(self.gw.handle_control({"op": "add_token", "token": again}),
                         {"ok": True, "existing": True})
        self.assertEqual(self.gw.tokens[cid].requests, 1)

    def test_an_unservable_profile_refuses_its_token_alone(self):
        good, good_row = self.row()
        bad, bad_row = self.row(self.profile(name="cf", provider="cloudflare"), persona="sme")
        reply = self.gw.handle_control({"op": "hello", "tokens": [good_row, bad_row]})
        self.assertEqual(reply["tokens"], 1)
        self.assertEqual(reply["refused"], [{"credential_id": bad_row["credential_id"],
                                             "reason": "profiles.cf: cloudflare profiles are not "
                                             "served by the hosted gateway until phase 2a.6"}])
        self.assertTrue(any("refused" in line for line in self.logs))
        self.assertEqual(self.post(good)[0], 200)
        self.assertEqual(self.post(bad)[0], 401)

    def test_a_token_keeps_the_route_it_was_dispatched_with(self):
        # The same credential, re-sent with a narrower route (PR #19 review):
        # a second token would leave the first untouched by construction.
        old_token, cid = self.hello()
        new_row = dict(self.row(self.profile(per_pair_requests=1), persona="sme")[1],
                       credential_id=cid)
        self.gw.handle_control({"op": "add_token", "token": new_row})
        for _ in range(3):
            self.assertEqual(self.post(old_token)[0], 200)

    # -- the route ------------------------------------------------------------------
    def test_head_api_hello_is_answered_without_upstream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("HEAD", "/api/hello")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual(self.up.seen, [])

    def test_other_paths_are_refused(self):
        token, _ = self.hello()
        for path in ("/v1/messages/batches", "/v1/files", "/v1/complete"):
            with self.subTest(path=path):
                self.assertEqual(self.post(token, path=path)[0], 404)
        self.assertEqual(self.up.seen, [])

    def test_an_unknown_wrong_or_revoked_token_is_refused(self):
        token, cid = self.hello()
        self.assertEqual(self.post("cbx_p_0000000000000000_" + "A" * 43)[0], 401)
        self.assertEqual(self.post(token[:-1] + ("A" if token[-1] != "A" else "B"))[0], 401)
        self.gw.handle_control({"op": "revoke", "credential_ids": [cid]})
        self.assertEqual(self.post(token)[0], 401)
        self.assertEqual(self.up.seen, [])

    def test_a_forwarded_request_carries_the_real_credential_and_the_jobs_model(self):
        token, _ = self.hello()
        status, body = self.post(token)
        self.assertEqual((status, body), (200, SSE_OK))
        seen = self.up.seen[0]
        self.assertEqual(seen["path"], "/v1/messages?beta=true")
        self.assertEqual(seen["headers"]["authorization"], "Bearer real-upstream-key")
        self.assertNotIn(token, json.dumps(seen))
        self.assertEqual(seen["body"]["model"], "the-model")

    def test_a_token_in_x_api_key_is_accepted_and_not_forwarded(self):
        token, _ = self.hello()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request("POST", "/v1/messages", body=b'{"messages": []}',
                     headers={"x-api-key": token, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertNotIn(token, json.dumps(self.up.seen[0]))

    # -- the record -------------------------------------------------------------------
    def test_usage_is_counted_and_reported_in_sequence(self):
        token, cid = self.hello()
        self.post(token)
        self.post(token)
        self.assertEqual(self.flush(cid), {"last_seq": 2, "drained": True})
        self.assertEqual([r["seq"] for r in self.reports], [1, 2])
        self.assertEqual(self.reports[0]["delta"], {"requests": 1, "successes": 1,
                                                    "input_tokens": 120, "output_tokens": 30})

    def test_a_flush_that_cannot_drain_says_so_and_names_the_seq_emitted(self):
        token, cid = self.hello()
        self.post(token)
        self.assertEqual(self.flush(cid), {"last_seq": 1, "drained": True})
        self.control_up = False
        self.post(token)
        # The control plane committed seq 1; the gateway emitted 2 and could not deliver it.
        self.assertEqual(self.flush(cid), {"last_seq": 2, "drained": False})
        self.assertEqual(self.reporter.acked(cid), 1)

    def test_max_tokens_is_clamped_to_what_remains(self):
        token, _ = self.hello()
        self.post(token, body={"max_tokens": 5000, "messages": []})  # spends 150 of 1000
        self.post(token, body={"max_tokens": 5000, "messages": []})
        self.assertEqual(self.up.seen[0]["body"]["max_tokens"], 1000)
        self.assertEqual(self.up.seen[1]["body"]["max_tokens"], 850)

    def test_a_spent_token_cap_ends_the_pair_as_capped_and_reports_it_once(self):
        token, cid = self.hello(counts={"input_tokens": 600, "output_tokens": 400})
        bodies = []
        for _ in range(3):
            status, body = self.post(token)
            self.assertEqual(status, 400)
            bodies.append(body)
        self.assertIn(b"tokens cap", bodies[0])
        self.assertTrue(all(b"reached a cap" in b for b in bodies[1:]))
        self.assertEqual(self.up.seen, [])
        self.assertEqual(self.flush(cid), {"last_seq": 1, "drained": True})
        self.assertEqual([r.get("cap_hit") for r in self.reports], ["tokens"])

    def test_the_request_cap_holds_across_a_hello(self):
        token, cid = self.hello(counts={"requests": 3, "seq": 9})
        status, body = self.post(token)
        self.assertEqual(status, 400)
        self.assertIn(b"requests cap", body)
        self.assertEqual(self.flush(cid)["last_seq"], 10)
        self.assertEqual(self.reports[-1]["cap_hit"], "requests")

    def test_concurrent_requests_cannot_outrun_the_request_cap(self):
        token, cid = self.hello()   # per_pair_requests 3
        self.up.delay = 0.2
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.post(token)[0]))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.up.seen), 3)
        self.assertEqual(sorted(results), [200, 200, 200, 400, 400, 400, 400, 400])

    def test_concurrent_requests_cannot_outrun_the_token_cap(self):
        token, cid = self.hello(self.profile(per_pair_requests=100))
        self.up.delay = 0.2
        threads = [threading.Thread(target=self.post, args=(token,),
                                    kwargs={"body": {"max_tokens": 400, "messages": []}})
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        granted = sum(s["body"]["max_tokens"] for s in self.up.seen)
        self.assertLessEqual(granted, 1000)
        # A shortfall decided by reservations is pacing, not a cap hit.
        self.flush(cid)
        self.assertFalse(any(r.get("cap_hit") for r in self.reports))
        self.up.delay = 0
        self.assertEqual(self.post(token)[0], 200)

    def test_a_cap_hit_is_terminal_even_after_usage_would_allow_more(self):
        token, cid = self.hello(self.profile(per_pair_requests=1))
        self.assertEqual(self.post(token)[0], 200)
        self.assertEqual(self.post(token)[0], 400)   # requests cap, reported
        self.gw.tokens[cid].requests = 0             # anything that would reopen it
        status, body = self.post(token)
        self.assertEqual(status, 400)
        self.assertIn(b"reached a cap", body)
        self.assertEqual(len(self.up.seen), 1)

    def test_a_token_past_its_in_flight_bound_is_paced_and_nothing_is_recorded(self):
        token, cid = self.hello(self.profile(per_pair_requests=100, per_pair_tokens=None,
                                             budget_unit="passes"))
        self.up.delay = 0.3
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.post(token)[0]))
                   for _ in range(gateway.IN_FLIGHT_MAX + 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(self.up.peak, gateway.IN_FLIGHT_MAX)
        self.assertIn(429, results)
        self.flush(cid)
        self.assertEqual(sum(r["delta"]["requests"] for r in self.reports), len(self.up.seen))

    def test_a_passes_profile_clamps_to_the_ceiling(self):
        token, _ = self.hello(self.profile(budget_unit="passes", per_pair_tokens=None))
        self.post(token, body={"max_tokens": 10 ** 9, "messages": []})
        self.assertEqual(self.up.seen[0]["body"]["max_tokens"], gateway.MAX_TOKENS_CEILING)

    def test_a_usage_limit_is_recorded_once(self):
        token, cid = self.hello()
        self.up.reply = (429, "application/json",
                         b'{"type":"error","error":{"type":"rate_limit_error"}}')
        self.post(token)
        self.post(token)
        self.flush(cid)
        self.assertEqual([r["first_limit"] for r in self.reports if "first_limit" in r],
                         ["upstream 429 rate_limit_error"])

    def test_an_upstream_5xx_is_a_failure(self):
        token, cid = self.hello()
        self.up.reply = (502, "text/plain", b"bad gateway")
        self.assertEqual(self.post(token)[0], 502)
        self.flush(cid)
        self.assertEqual(self.reports[-1]["first_failure"], "upstream 502")
        self.assertEqual(self.reports[-1]["delta"]["successes"], 0)

    def test_a_request_the_provider_rejects_is_not_a_provider_failure(self):
        token, cid = self.hello()
        self.up.reply = (400, "application/json",
                         b'{"type":"error","error":{"type":"invalid_request_error"}}')
        self.assertEqual(self.post(token)[0], 400)
        self.flush(cid)
        self.assertEqual(self.reports[-1]["first_rejected"], "upstream 400 invalid_request_error")
        self.assertNotIn("first_failure", self.reports[-1])

    def test_a_refused_credential_is_a_provider_failure(self):
        token, cid = self.hello()
        self.up.reply = (401, "application/json", b'{"type":"error","error":{"type":"x"}}')
        self.post(token)
        self.flush(cid)
        self.assertEqual(self.reports[-1]["first_failure"], "upstream 401 x")

    def test_an_unreachable_provider_is_a_transport_failure(self):
        token, cid = self.hello(self.profile(base_url="http://127.0.0.1:9"))
        self.assertEqual(self.post(token)[0], 502)
        self.flush(cid)
        self.assertTrue(self.reports[-1]["first_failure"].startswith("upstream transport: "))

    def test_a_response_the_provider_cuts_off_is_a_failure_not_a_success(self):
        token, cid = self.hello()
        self.up.truncate = True
        self.post_tolerant(token)
        self.flush(cid)
        self.assertTrue(self.reports[-1]["first_failure"].startswith("upstream transport: "))
        self.assertEqual(self.reports[-1]["delta"]["successes"], 0)

    def test_a_stream_that_ends_before_message_stop_is_a_failure(self):
        token, cid = self.hello()
        self.up.reply = (200, "text/event-stream", SSE_NO_STOP)
        self.post(token)
        self.flush(cid)
        self.assertIn("ended before message_stop", self.reports[-1]["first_failure"])
        self.assertEqual(self.reports[-1]["delta"]["input_tokens"], 120)

    def post_tolerant(self, token):
        try:
            return self.post(token)
        except (http.client.HTTPException, OSError):
            return None

    def test_a_worker_that_hangs_up_is_counted_and_concludes_nothing(self):
        token, cid = self.hello()
        self.up.delay = 0.2
        conn = socket.create_connection(("127.0.0.1", self.server.server_port))
        body = b'{"messages": [], "max_tokens": 10}'
        conn.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                     + token.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\n\r\n" + body)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        conn.close()
        time.sleep(0.5)
        self.flush(cid)
        report = self.reports[-1]
        self.assertEqual(report["delta"], {"requests": 1, "successes": 0,
                                           "input_tokens": 0, "output_tokens": 0})
        self.assertEqual(report["first_abandoned"], "the worker hung up mid-response")
        for key in ("first_failure", "first_limit", "first_rejected"):
            self.assertNotIn(key, report)
        # Its spend was never seen, so a tokens profile fails closed and pages.
        self.assertTrue(report["usage_missing"])
        self.assertTrue(any(line.startswith("PAGE: ") for line in self.logs))

    def test_a_hangup_after_a_429_still_records_the_limit(self):
        # PR #19 review: the status was read before the worker left.
        token, cid = self.hello()
        self.up.reply = (429, "application/json",
                         b'{"type":"error","error":{"type":"rate_limit_error"}}' + b" " * 300000)
        conn = socket.create_connection(("127.0.0.1", self.server.server_port))
        body = b'{"messages": [], "max_tokens": 10}'
        conn.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                     + token.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\n\r\n" + body)
        conn.recv(64)   # the status line has been relayed
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        conn.close()
        time.sleep(0.5)
        self.flush(cid)
        report = self.reports[-1]
        self.assertIn("first_limit", report)
        self.assertNotIn("first_abandoned", report)

    def test_a_flush_never_reads_a_seq_whose_report_is_not_queued(self):
        # PR #19 review: seq and enqueue are one step under the gateway's lock.
        token, cid = self.hello()
        state = next(iter(self.gw.tokens.values()))
        real = self.gw.reporter.enqueue
        seen = []

        def enqueue(report):
            seen.append(self.gw.lock.acquire(blocking=False) and (self.gw.lock.release() or True))
            return real(report)
        self.gw.reporter.enqueue = enqueue
        self.assertEqual(self.post(token)[0], 200)   # complete()
        self.gw.refuse_cap(state, "requests")
        self.assertEqual(len(seen), 2)
        self.assertFalse(any(seen))   # every enqueue ran while seq's lock was held

    def test_a_flush_waits_for_a_request_still_in_flight(self):
        # PR #19 review: an empty queue is not a quiet token.
        token, cid = self.hello()
        self.up.delay = 0.15
        t = threading.Thread(target=self.post, args=(token,))
        t.start()
        time.sleep(0.05)
        self.assertEqual(self.flush(cid), {"last_seq": 1, "drained": True})
        t.join()

    def test_a_request_that_outlasts_the_flush_is_not_drained(self):
        token, cid = self.hello()
        self.up.delay = 0.6
        t = threading.Thread(target=self.post, args=(token,))
        t.start()
        time.sleep(0.05)
        self.assertFalse(self.flush(cid)["drained"])
        t.join()

    def test_a_hangup_on_a_passes_profile_is_abandoned_and_not_failed_closed(self):
        token, cid = self.hello(self.profile(budget_unit="passes", per_pair_tokens=None))
        self.up.delay = 0.2
        conn = socket.create_connection(("127.0.0.1", self.server.server_port))
        body = b'{"messages": []}'
        conn.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                     + token.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\n\r\n" + body)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        conn.close()
        time.sleep(0.5)
        self.flush(cid)
        self.assertIn("first_abandoned", self.reports[-1])
        self.assertNotIn("usage_missing", self.reports[-1])
        self.up.delay = 0
        self.assertEqual(self.post(token)[0], 200)

    def test_an_unexpected_fault_in_the_forward_releases_the_reservation(self):
        token, cid = self.hello(self.profile(base_url="http://127.0.0.1:notaport"))
        for _ in range(3):
            self.post_tolerant(token)
        state = self.gw.tokens[cid]
        self.assertEqual((state.in_flight, state.reserved_tokens), (0, 0))
        self.flush(cid)
        self.assertEqual([r.get("first_abandoned") for r in self.reports][:1],
                         ["gateway fault: ValueError"])
        self.assertNotIn("first_failure", self.reports[0])
        self.assertTrue(any(l.startswith("WARN: gateway fault") for l in self.logs))

    def test_a_tokens_profile_without_usage_fails_the_pair_closed_and_pages(self):
        token, cid = self.hello()
        self.up.reply = (200, "text/event-stream", SSE_NO_USAGE)
        self.assertEqual(self.post(token)[0], 200)
        self.flush(cid)
        self.assertTrue(self.reports[-1]["usage_missing"])
        self.assertTrue(any(line.startswith("PAGE: ") for line in self.logs))
        status, body = self.post(token)
        self.assertEqual(status, 400)
        self.assertIn(b"failed closed", body)
        self.assertEqual(len(self.up.seen), 1)

    def test_a_passes_profile_without_usage_is_fine(self):
        token, cid = self.hello(self.profile(budget_unit="passes", per_pair_tokens=None))
        self.up.reply = (200, "text/event-stream", SSE_NO_USAGE)
        self.post(token)
        self.assertEqual(self.post(token)[0], 200)
        self.flush(cid)
        self.assertFalse(any(r.get("usage_missing") for r in self.reports))

    def test_an_oversized_or_non_json_body_is_refused_without_upstream(self):
        token, cid = self.hello()
        # The gateway refuses on the declared length, before reading the body.
        self.assertEqual(self.post(token, raw=b"{}", length=gateway.BODY_MAX + 1)[0], 413)
        self.assertEqual(self.post(token, raw=b"[1,2]")[0], 400)
        self.assertEqual(self.up.seen, [])
        self.assertEqual(self.flush(cid)["last_seq"], 0)

    def test_reports_wait_for_the_control_plane_and_arrive_in_order(self):
        token, cid = self.hello()
        self.control_up = False
        self.post(token)
        self.post(token)
        time.sleep(0.2)
        self.assertEqual(self.reports, [])
        self.assertEqual(self.reporter.pending(cid), 2)
        self.control_up = True
        self.assertEqual(self.flush(cid), {"last_seq": 2, "drained": True})
        self.assertEqual([r["seq"] for r in self.reports], [1, 2])

    def test_an_unknown_control_op_is_an_error(self):
        self.hello()
        self.assertIn("error", self.gw.handle_control({"op": "drop_tables"}))


class ListenerTest(unittest.TestCase):
    def setUp(self):
        reporter = gateway.Reporter(lambda *a, **k: {"ack": 0})
        self.server = gateway.make_server(gateway.Gateway({}, reporter), "127.0.0.1", 0)
        self.server.max_connections = 3
        self.server.max_per_peer = 2
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_connections_past_the_bound_are_closed_at_once(self):
        idle = [socket.create_connection(("127.0.0.1", self.server.server_port)) for _ in range(2)]
        self.addCleanup(lambda: [s.close() for s in idle])
        time.sleep(0.1)
        extra = socket.create_connection(("127.0.0.1", self.server.server_port))
        extra.settimeout(2)
        self.assertEqual(extra.recv(1), b"")
        extra.close()

    def test_a_slot_is_given_back_when_its_connection_closes(self):
        for _ in range(5):
            s = socket.create_connection(("127.0.0.1", self.server.server_port))
            s.close()
        time.sleep(0.1)
        self.assertEqual(self.server._open, 0)
        self.assertEqual(self.server._per_peer, {})

    def test_the_overall_bound_holds_across_peers(self):
        self.server._open = 3
        self.assertFalse(self.server._take("10.0.0.9"))
        self.server._open = 0

    def test_an_idle_connection_has_a_timeout(self):
        self.assertEqual(gateway.Handler.timeout, gateway.IDLE_SECONDS)


class MainTest(unittest.TestCase):
    """gateway.main as the supervisor will run it: a process, two sockets, one port."""

    def test_the_process_serves_after_a_hello_and_drains_on_sigterm(self):
        import re
        import signal
        import subprocess
        import sys
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d)
        control = gwproto.Server(os.path.join(d, "c.sock"), "s", lambda m: {"ack": m["seq"]}).start()
        self.addCleanup(control.stop)
        env = {"PATH": os.environ.get("PATH", ""), "GATEWAY_SECRET": "s",
               "GATEWAY_SOCKET": os.path.join(d, "g.sock"),
               "CONTROL_SOCKET": os.path.join(d, "c.sock"),
               "GATEWAY_LISTEN": "127.0.0.1:0", "K": "real"}
        script = os.path.join(os.path.dirname(gateway.__file__), "gateway.py")
        proc = subprocess.Popen([sys.executable, script], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        line = proc.stdout.readline()
        port = int(re.search(r"listening on 127\.0\.0\.1:(\d+)", line).group(1))

        def post():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/messages", body=b"{}",
                         headers={"Authorization": "Bearer x", "Content-Length": "2"})
            status = conn.getresponse().status
            conn.close()
            return status

        self.assertEqual(post(), 503)
        client = gwproto.Client(os.path.join(d, "g.sock"), "s")
        self.assertEqual(client.call("hello", tokens=[]), {"ok": True, "tokens": 0, "refused": []})
        client.close()
        self.assertEqual(post(), 401)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertIn("reports drained", proc.stdout.read())



class ReporterIsolationTest(unittest.TestCase):
    def test_a_report_the_control_plane_refuses_stalls_only_its_own_token(self):
        # PR #19 review: one global head would stall every token behind it.
        lines = []

        def call(op, **report):
            return {"error": "x"} if report["credential_id"] == "a" else {"ack": report["seq"]}
        reporter = gateway.Reporter(call, retry_seconds=0.05, log=lines.append).start()
        self.addCleanup(reporter.stop)
        reporter.enqueue({"credential_id": "a", "seq": 1})
        reporter.enqueue({"credential_id": "b", "seq": 1})
        reporter.enqueue({"credential_id": "b", "seq": 2})
        self.assertTrue(reporter.flush("b", timeout=2))
        self.assertEqual(reporter.acked("b"), 2)
        self.assertFalse(reporter.flush("a", timeout=0.2))
        self.assertEqual(len([l for l in lines if "report 1 for a" in l]), 1)   # logged once
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_hosted_gateway.py'`
Expected: FAIL: `AttributeError: module 'gateway' has no attribute 'Reporter'`.

- [ ] **Step 3: Implement**

Append to `hosted/gateway.py`:

```python
@dataclass
class TokenState:
    credential_id: str
    salt_hex: str
    hash_hex: str
    job_id: str
    persona: str
    model: str
    expires_at: int
    route: Route
    seq: int = 0
    requests: int = 0
    successes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    first_limit: Optional[str] = None
    first_failure: Optional[str] = None
    first_rejected: Optional[str] = None
    first_abandoned: Optional[str] = None
    cap_hit: Optional[str] = None
    usage_missing: bool = False
    revoked: bool = False
    # Not reported: what admission has reserved for requests still in flight.
    in_flight: int = 0
    reserved_tokens: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any], env: Mapping[str, str],
                 upstreams: Mapping[str, str]) -> "TokenState":
        """A token row from hello or add_token. Raises ValueError for an unservable profile."""
        counts = row.get("counts") or {}
        route = Route.from_profile(row["route"], env, upstreams)
        return cls(row["credential_id"], row["salt_hex"], row["hash_hex"], row["job_id"],
                   row["persona"], row["model"], int(row["expires_at"]), route,
                   seq=int(counts.get("seq", 0)), requests=int(counts.get("requests", 0)),
                   successes=int(counts.get("successes", 0)),
                   input_tokens=int(counts.get("input_tokens", 0)),
                   output_tokens=int(counts.get("output_tokens", 0)),
                   first_limit=counts.get("first_limit"),
                   first_failure=counts.get("first_failure"),
                   first_rejected=counts.get("first_rejected"),
                   first_abandoned=counts.get("first_abandoned"),
                   cap_hit=counts.get("cap_hit"),
                   usage_missing=bool(counts.get("usage_missing", False)))


class Reporter:
    """Sends reports to the control plane in order per token, buffering while it is away.

    Reports are deltas, so a token's order matters: each token has its own
    FIFO, and its head stays there until acknowledged. One thread visits the
    tokens in turn, so a report the control plane will not acknowledge stalls
    its own token's flush and no other. Every report is one admitted request or
    one cap hit, so a token's share is bounded by its caps, and what a crash
    loses is bounded the same way.
    """

    def __init__(self, call: Callable[..., Dict[str, Any]], retry_seconds: float = 1.0,
                 log: Callable[[str], None] = lambda line: None):
        self._call = call
        self._retry = retry_seconds
        self._log = log
        self._queues: Dict[str, Deque[Dict[str, Any]]] = {}
        self._retry_at: Dict[str, float] = {}
        self._refused: Dict[str, int] = {}   # token -> the seq last logged as refused
        self._acked: Dict[str, int] = {}
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stopping = False

    def start(self) -> "Reporter":
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def enqueue(self, report: Dict[str, Any]) -> None:
        with self._cond:
            self._queues.setdefault(report["credential_id"], deque()).append(report)
            self._cond.notify_all()

    def pending(self, credential_id: Optional[str] = None) -> int:
        with self._cond:
            return sum(len(q) for cid, q in self._queues.items()
                       if credential_id is None or cid == credential_id)

    def acked(self, credential_id: str) -> int:
        with self._cond:
            return self._acked.get(credential_id, 0)

    def flush(self, credential_id: Optional[str], timeout: float = FLUSH_SECONDS) -> bool:
        """Wait until nothing is queued for this token (or at all, for None). True if drained."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while any(q for cid, q in self._queues.items()
                      if credential_id is None or cid == credential_id):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stopping:
                        return
                    now = time.monotonic()
                    ready = [cid for cid, q in self._queues.items()
                             if q and self._retry_at.get(cid, 0) <= now]
                    if ready:
                        break
                    waits = [self._retry_at[cid] - now for cid, q in self._queues.items() if q]
                    self._cond.wait(min(waits) if waits else None)
                heads = [self._queues[cid][0] for cid in ready]
            for head in heads:
                cid = head["credential_id"]
                try:
                    reply = self._call("report", **head)
                    ok = reply.get("ack") == head["seq"]
                    refused = not ok
                except (OSError, gwproto.ProtocolError):
                    ok = refused = False   # the control plane is away: every token waits alike
                with self._cond:
                    if ok:
                        self._queues[cid].popleft()
                        if not self._queues[cid]:
                            del self._queues[cid]
                        self._retry_at.pop(cid, None)
                        self._acked[cid] = head["seq"]
                        self._cond.notify_all()
                        continue
                    self._retry_at[cid] = time.monotonic() + self._retry
                    log_it = refused and self._refused.get(cid) != head["seq"]
                    if log_it:
                        self._refused[cid] = head["seq"]
                if log_it:
                    self._log(f"WARN: the control plane answered report {head['seq']} for "
                              f"{cid} without acknowledging it ({reply!r:.200}); that token's "
                              f"reports wait, and no other's")


class Gateway:
    """The provider route's state: tokens, each with its route, and the reporter."""

    def __init__(self, env: Mapping[str, str], reporter: Reporter,
                 upstreams: Mapping[str, str] = UPSTREAMS,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: print(line, flush=True),
                 flush_seconds: float = FLUSH_SECONDS):
        self.env = env
        self.flush_seconds = flush_seconds
        self.reporter = reporter
        self.upstreams = dict(upstreams)
        self.clock = clock
        self.log = log
        self.ready = False
        self.tokens: Dict[str, TokenState] = {}
        self.lock = threading.Lock()

    # -- the control plane's side (gateway.sock) --------------------------------
    def _build(self, rows: List[Mapping[str, Any]]) -> Tuple[Dict[str, TokenState], List[dict]]:
        built, refused = {}, []
        for row in rows:
            try:
                built[row["credential_id"]] = TokenState.from_row(row, self.env, self.upstreams)
            except (ValueError, KeyError, TypeError) as exc:
                refused.append({"credential_id": row.get("credential_id"), "reason": str(exc)})
                self.log(f"WARN: token {row.get('credential_id')} refused: {exc}")
        return built, refused

    def handle_control(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        op = msg.get("op")
        if op == "hello":
            # A full snapshot of the live tokens. One the gateway already holds
            # keeps whichever copy has the higher seq, so a hello never regresses
            # a count it has moved past; one the snapshot leaves out is dropped,
            # since the control plane revoked it.
            built, refused = self._build(msg.get("tokens", []))
            with self.lock:
                for cid, fresh in built.items():
                    held = self.tokens.get(cid)
                    if held is not None and held.seq >= fresh.seq:
                        built[cid] = held
                self.tokens, self.ready = built, True
            return {"ok": True, "tokens": len(built), "refused": refused}
        if not self.ready:
            # The control plane answers this by sending a hello: it is how a
            # gateway that restarted alone gets its tokens back.
            return {"error": "hello first", "ready": False}
        if op == "add_token":
            cid = msg["token"]["credential_id"]
            with self.lock:
                if cid in self.tokens:
                    return {"ok": True, "existing": True}
            built, refused = self._build([msg["token"]])
            if refused:
                return {"error": refused[0]["reason"]}
            with self.lock:
                self.tokens.setdefault(cid, built[cid])
            return {"ok": True}
        if op == "revoke":
            with self.lock:
                for cid in msg.get("credential_ids", []):
                    if cid in self.tokens:
                        self.tokens[cid].revoked = True
            return {"ok": True}
        if op == "flush":
            # The last seq this gateway EMITTED for the token, after waiting for
            # the queue to drain. The control plane compares it with the last seq
            # it committed: equal means it holds every report; a gap, or
            # drained false, means the flush did not complete.
            # Drained means quiet as well as sent: a request still in flight
            # has no seq yet, and its report may be the one that decides the
            # pair (a hang-up the gateway has not yet seen). A pair's worker
            # sends nothing more once its pass is over, so this waits out
            # requests already admitted, within the same budget.
            cid = msg["credential_id"]
            deadline = time.monotonic() + self.flush_seconds
            while True:
                with self.lock:
                    state = self.tokens.get(cid)
                    busy = state is not None and state.in_flight > 0
                if not busy or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            drained = not busy and self.reporter.flush(
                cid, timeout=max(0.0, deadline - time.monotonic()))
            with self.lock:
                state = self.tokens.get(cid)
                return {"last_seq": state.seq if state else 0, "drained": drained}
        return {"error": f"unknown op {op!r}"}

    # -- the worker's side --------------------------------------------------------
    def find(self, token: str) -> Optional[TokenState]:
        cid = creds.credential_id_of(token)
        with self.lock:
            state = self.tokens.get(cid) if cid else None
        if state is None or state.revoked or self.clock() >= state.expires_at:
            return None
        if not creds.verify_against(token, state.salt_hex, state.hash_hex):
            return None
        return state

    def admit(self, state: TokenState, path: str, requested_max: Any) -> Tuple[Optional[str], int]:
        """Reserve one request, and its max_tokens, before forwarding.

        Returns (refusal, max_tokens), and reserves nothing on a refusal. A
        refusal is "capped" (a cap was already hit: the pair is done), a cap
        name ("requests" or "tokens", hit now, by counted use), or "pacing"
        (too many in flight, or what is left is all reserved by requests
        still running: wait, nothing is concluded). Under the lock, so
        concurrent requests on one token serialize on the check instead of
        all passing it.
        """
        route = state.route
        with self.lock:
            if state.cap_hit is not None:
                return "capped", 0
            if state.requests >= route.per_pair_requests:
                return "requests", 0
            used = state.input_tokens + state.output_tokens
            if path == "/v1/messages" and route.per_pair_tokens is not None \
                    and used >= route.per_pair_tokens:
                return "tokens", 0
            if state.in_flight >= IN_FLIGHT_MAX:
                return "pacing", 0
            granted = 0
            if path == "/v1/messages":
                ceiling = MAX_TOKENS_CEILING
                if route.per_pair_tokens is not None:
                    ceiling = route.per_pair_tokens - used - state.reserved_tokens
                    if ceiling <= 0:
                        return "pacing", 0
                granted = requested_max if isinstance(requested_max, int) and \
                    not isinstance(requested_max, bool) and 0 < requested_max <= ceiling else ceiling
                if route.per_pair_tokens is not None:
                    state.reserved_tokens += granted
            state.requests += 1
            state.in_flight += 1
            return None, granted

    def refuse_cap(self, state: TokenState, cap: str) -> None:
        """Report a cap hit, once. Later refusals on the same token report nothing."""
        with self.lock:
            if state.cap_hit is not None:
                return
            state.cap_hit = cap
            state.seq += 1
            report = {"credential_id": state.credential_id, "seq": state.seq, "cap_hit": cap,
                      "delta": {"requests": 0, "successes": 0, "input_tokens": 0,
                                "output_tokens": 0}}
            # Queued under the lock that bumped seq, so a flush that finds the
            # queue empty never reads a seq whose report is not yet in it.
            self.reporter.enqueue(report)

    def complete(self, state: TokenState, kind: str, granted: int,
                 meter: Optional[UsageMeter] = None, detail: str = "") -> None:
        """Release an admitted request's reservation and report what it did.

        `kind` is classify()'s, or "abandoned" when the worker hung up before
        the response was relayed in full. An abandoned request is reported as
        `first_abandoned`, which the control plane commits as `deferred`, never
        `ok`. On a tokens profile, an abandoned request whose usage was never
        seen is also `usage_missing`: its spend cannot be counted, so the pair
        is failed closed and paged, the same as a success without usage.
        """
        route = state.route
        report: Dict[str, Any] = {}
        with self.lock:
            state.in_flight -= 1
            if route.per_pair_tokens is not None:
                state.reserved_tokens -= granted
            delta = {"requests": 1, "successes": 0, "input_tokens": 0, "output_tokens": 0}
            if meter is not None:
                state.input_tokens += meter.input_tokens
                state.output_tokens += meter.output_tokens
                delta["input_tokens"], delta["output_tokens"] = meter.input_tokens, meter.output_tokens
            if kind in ("success", "abandoned") and route.tokens_unit and meter is not None \
                    and not meter.saw_usage and not state.usage_missing:
                state.usage_missing = True
                report["usage_missing"] = True
            if kind == "success":
                state.successes += 1
                delta["successes"] = 1
            elif kind == "abandoned" and state.first_abandoned is None:
                state.first_abandoned = report["first_abandoned"] = detail or "the worker hung up"
            elif kind == "limit" and state.first_limit is None:
                state.first_limit = report["first_limit"] = detail
            elif kind == "failure" and state.first_failure is None:
                state.first_failure = report["first_failure"] = detail
            elif kind == "rejected" and state.first_rejected is None:
                state.first_rejected = report["first_rejected"] = detail
            state.seq += 1
            report.update(credential_id=state.credential_id, seq=state.seq, delta=delta)
            self.reporter.enqueue(report)   # under the lock: see refuse_cap
        if report.get("usage_missing"):
            self.log(f"PAGE: profile {route.name} reports tokens but a response for "
                     f"{state.job_id}/{state.persona} carried no usage; that pair is failed closed")


def _error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


class _WorkerGone(Exception):
    """The worker's connection broke while the response was being relayed."""


class Handler(http.server.BaseHTTPRequestHandler):
    gateway: Gateway  # set on the subclass make_server builds
    server_version = "claudebox-gateway"
    sys_version = ""
    timeout = IDLE_SECONDS

    def log_message(self, fmt, *args):  # no request lines with tokens in them
        pass

    def _send(self, status: int, body: bytes = b"", kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _refuse(self, status: int, kind: str, message: str) -> None:
        self._send(status, _error_body(kind, f"claudebox gateway: {message}"))

    def do_HEAD(self):
        # Claude Code probes HEAD /api/hello at startup (spike S3). Answer it here
        # without going upstream; nothing else is served to HEAD.
        if urllib.parse.urlsplit(self.path).path == "/api/hello":
            self._send(200)
        else:
            self._send(404)

    def do_GET(self):
        self._refuse(404, "not_found_error", "only POST /v1/messages and /v1/messages/count_tokens")

    def do_POST(self):
        gw = self.gateway
        path = urllib.parse.urlsplit(self.path).path
        if not gw.ready:
            return self._refuse(503, "overloaded_error", "starting; no provider traffic yet")
        if path not in ROUTES:
            return self._refuse(404, "not_found_error",
                                "only POST /v1/messages and /v1/messages/count_tokens")
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        state = gw.find(token)
        if state is None:
            return self._refuse(401, "authentication_error", "unknown, expired or revoked token")
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self._refuse(411, "invalid_request_error", "a Content-Length is required")
        if int(length) > BODY_MAX:
            # The spec's cap table: "request refused". Not a pair outcome.
            return self._refuse(413, "request_too_large", f"body over {BODY_MAX} bytes")
        try:
            body = json.loads(self.rfile.read(int(length)))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._refuse(400, "invalid_request_error", "body is not a JSON object")

        if state.usage_missing:
            return self._refuse(400, "invalid_request_error",
                                "this pair was failed closed: a response carried no usage")
        refusal, granted = gw.admit(state, path, body.get("max_tokens"))
        if refusal == "pacing":
            # Not an outcome: nothing reached upstream, so nothing is recorded.
            return self._refuse(429, "rate_limit_error",
                                "this pair's requests in flight hold its whole allowance; retry")
        if refusal == "capped":
            return self._refuse(400, "invalid_request_error", "this pair reached a cap; it is done")
        if refusal is not None:
            gw.refuse_cap(state, refusal)
            return self._refuse(400, "invalid_request_error",
                                f"this pair reached its {refusal} cap")
        body["model"] = state.model
        if path == "/v1/messages":
            body["max_tokens"] = granted
        # The reservation admit() took is released on every path, including an
        # exception nobody listed: without that, a request that died here would
        # hold its pair's in-flight slot and reserved tokens for the life of the
        # process, and the record would never hear of it.
        self._completed = False
        try:
            self._forward(state, path, json.dumps(body).encode("utf-8"), granted)
        finally:
            if not self._completed:
                exc = sys.exc_info()[1]
                fault = type(exc).__name__ if exc is not None else "no outcome"
                gw.log(f"WARN: gateway fault forwarding for job {state.job_id}: {fault}")
                gw.complete(state, "abandoned", granted, detail=f"gateway fault: {fault}")

    def _complete(self, state: TokenState, kind: str, granted: int,
                  meter: Optional[UsageMeter] = None, detail: str = "") -> None:
        self._completed = True
        self.gateway.complete(state, kind, granted, meter, detail=detail)

    def _forward(self, state: TokenState, path: str, payload: bytes, granted: int) -> None:
        gw = self.gateway
        route = state.route
        target = urllib.parse.urlsplit(route.base_url)
        query = urllib.parse.urlsplit(self.path).query
        upstream_path = (target.path.rstrip("/") + path) + (f"?{query}" if query else "")
        if target.scheme == "https":
            conn = http.client.HTTPSConnection(target.hostname, target.port or 443,
                                               timeout=600, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=600)
        headers = upstream_headers(route, gw.env.get(route.credential, ""), self.headers,
                                   len(payload))
        try:
            conn.request("POST", upstream_path, body=payload, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            conn.close()
            self._complete(state, "failure", granted, detail=f"upstream transport: {type(exc).__name__}")
            return self._refuse(502, "api_error", "the provider could not be reached")

        meter = UsageMeter("text/event-stream" in (resp.getheader("Content-Type") or ""))
        broke: Optional[str] = None
        try:
            self._relay_head(resp)
            while True:
                try:
                    chunk = resp.read1(65536)
                except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                    broke = type(exc).__name__   # upstream broke mid-response
                    break
                if not chunk:
                    break
                meter.feed(chunk)
                self._relay(chunk)
        except _WorkerGone:
            conn.close()
            meter.finish()
            # The status line was read before the worker left. A non-2xx is
            # decided by its status alone, so it is recorded as what upstream
            # did; only a lost 2xx, whose outcome needs the body, is abandoned.
            if resp.status >= 300:
                kind = classify(path, resp.status, meter)
                self._complete(state, kind, granted, meter, detail=f"upstream {resp.status}"
                               + (f" {meter.error_type}" if meter.error_type else ""))
            else:
                self._complete(state, "abandoned", granted, meter,
                               detail="the worker hung up mid-response")
            return
        conn.close()
        meter.finish()
        if broke is not None:
            kind = "limit" if meter.error_type in LIMIT_TYPES else "failure"
            self._complete(state, kind, granted, meter, detail=f"upstream transport: {broke}")
            return
        kind = classify(path, resp.status, meter)
        detail = f"upstream {resp.status}" + (f" {meter.error_type}" if meter.error_type else "")
        if kind == "failure" and resp.status < 300 and meter.error_type is None:
            detail = f"upstream {resp.status}: the stream ended before message_stop"
        self._complete(state, kind, granted, meter, detail=detail)

    def _relay_head(self, resp) -> None:
        try:
            self.send_response(resp.status)
            for name in _PASS_DOWN:
                value = resp.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            raise _WorkerGone() from None

    def _relay(self, chunk: bytes) -> None:
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except OSError:
            raise _WorkerGone() from None


class BoundedServer(http.server.ThreadingHTTPServer):
    """A ThreadingHTTPServer with a connection bound overall and per peer.

    Both are charged at accept, before a byte of the request is read, so a
    client that never sends a token is bounded as tightly as one that does.
    """

    daemon_threads = True
    max_connections = MAX_CONNECTIONS
    max_per_peer = MAX_CONNECTIONS_PER_PEER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._count_lock = threading.Lock()
        self._open = 0
        self._per_peer: Dict[str, int] = {}

    def _take(self, peer: str) -> bool:
        with self._count_lock:
            if self._open >= self.max_connections or \
                    self._per_peer.get(peer, 0) >= self.max_per_peer:
                return False
            self._open += 1
            self._per_peer[peer] = self._per_peer.get(peer, 0) + 1
            return True

    def _give(self, peer: str) -> None:
        with self._count_lock:
            self._open -= 1
            left = self._per_peer.get(peer, 1) - 1
            if left:
                self._per_peer[peer] = left
            else:
                self._per_peer.pop(peer, None)

    def process_request(self, request, client_address):
        peer = client_address[0]
        if not self._take(peer):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._give(peer)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._give(client_address[0])


def make_server(gateway: Gateway, host: str, port: int) -> BoundedServer:
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    return BoundedServer((host, port), handler)


def main(env: Mapping[str, str] = os.environ) -> None:
    """Run the gateway: GATEWAY_SECRET, GATEWAY_SOCKET, CONTROL_SOCKET, GATEWAY_LISTEN.

    On SIGTERM it stops accepting requests and gives queued reports up to
    DRAIN_SECONDS to reach the control plane before it exits, so a clean stop
    loses none of them.
    """
    secret = env["GATEWAY_SECRET"]
    client = gwproto.Client(env["CONTROL_SOCKET"], secret)
    reporter = Reporter(client.call, log=lambda line: print(line, flush=True)).start()
    gateway = Gateway(env, reporter)
    control = gwproto.Server(env["GATEWAY_SOCKET"], secret, gateway.handle_control).start()
    host, _, port = env.get("GATEWAY_LISTEN", "0.0.0.0:8090").rpartition(":")
    server = make_server(gateway, host or "0.0.0.0", int(port))
    signal.signal(signal.SIGTERM,
                  lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    bound_host, bound_port = server.server_address[:2]
    print(f"gateway listening on {bound_host}:{bound_port}; waiting for the control plane's "
          "hello", flush=True)
    try:
        server.serve_forever()
    finally:
        drained = reporter.flush(None, timeout=DRAIN_SECONDS)
        print(f"gateway stopping; reports {'drained' if drained else 'NOT drained'} "
              f"({reporter.pending()} left)", flush=True)
        control.stop()
        reporter.stop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_gateway.py'`, then `./test-python.sh`
Expected: PASS, 61 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/gateway.py tests/test_hosted_gateway.py
git commit -m "hosted: the gateway process, the author of every pair's provider outcome"
```

### Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (a paragraph in "Hosted App libraries", after the 2a.1 paragraph)
- Modify: `HISTORY.md` (one entry at the top of "Unreleased")

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: nothing code depends on.

- [ ] **Step 1: Describe the gateway**

In `CLAUDE.md`'s "Hosted App libraries" subsection, after the paragraph about the 2a.1 libraries, add:

```markdown
Phase 2a.2 adds the gateway, the first hosted process. `gateway.py` is the worker's only route to a model provider. A worker's `claude` points `ANTHROPIC_BASE_URL` at it and presents a per-pair provider token. The gateway checks the token against rows the control plane handed it, overwrites the model, enforces the pair's request and token caps, swaps the token for the profile's real credential, and streams the response back unbuffered. It answers Claude Code's `HEAD /api/hello` itself, and refuses every path except `POST /v1/messages` and `/v1/messages/count_tokens`. It is the author of each pair's provider outcome: it counts successes, tokens, the first usage limit, the first failure and any cap hit, and reports each change to the control plane over a unix socket, numbered per token and buffered while the control plane is away. It serves nothing until the control plane's `hello` has handed it the live tokens and their committed counts, so a restart never resets a cap. `gwproto.py` is the socket protocol: line-delimited JSON with the per-deployment secret on every message in both directions. It serves `ollama`, `anthropic` (API key or OAuth token) and `custom` profiles. `cloudflare` and `workersai` follow in 2a.6. A hosted profile has no environment to infer from, so `deploy_config` gained `base_url` (required on `custom`) and `auth` (how the credential is presented).
```

- [ ] **Step 2: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.2 of the hosted GitHub App: the gateway.** The process a hosted worker reaches its model provider through, so no worker ever holds the provider credential. It checks each request's per-pair token, pins the model, enforces the pair's request and token caps, injects the real credential (an Anthropic API key or OAuth token, an Ollama key, or a custom upstream's bearer or `x-api-key`), and streams the response through unbuffered. It decides each pair's provider outcome from what it sees, and reports it to the control plane in order, buffering while the control plane is down. It serves nothing until the control plane has told it the live tokens and their counts so far. Deployment profiles gained `base_url` and `auth`, which a `custom` or OAuth-token profile needs. Local mode is unchanged.
```

- [ ] **Step 3: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && ./test-launcher.sh`
Expected: all five pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md HISTORY.md
git commit -m "docs: hosted phase 2a.2, the gateway"
```
