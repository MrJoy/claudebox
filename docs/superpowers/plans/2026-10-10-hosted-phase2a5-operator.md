# Hosted Phase 2a.5: The Operator Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** What an operator runs to bring a deployment up and keep it. That is `hosted deploy --target local [--admins FILE]`, `app create --org ORG --target local`, `hosted whois`, `hosted login`, `hosted status` and `hosted config apply`, plus the control plane's admin API behind the last three. At the end of this plan an org can create the App, install it, and get its first review from a laptop. That review is this plan's last task, run by a person against a throwaway org.

**Architecture:** `admin.py` is a fourth HTTP server in the control-plane process. It starts with `/healthz`, checks each GitHub user token with GitHub as one this App issued, matches the numeric user id against the role lists the platform hands it, and serves `status` and `config apply` from the control plane's memory and records store. `hosted_cli.py` is the in-image half of every new command. It runs in a throwaway container of the control-plane image, as `config validate` runs `config_cli.py`, and it prints on stdout only what the launcher keeps (an App's secrets, a user token, a checked roles line). `claudebox.sh` gains the `hosted` and `app` commands, which drive Docker Compose, the env file and that container. Phase 1's F7 lands in the same file.

**Tech Stack:** Python 3.9+ standard library (`http.server`, `urllib`, `hashlib`, `hmac`, `secrets`, `unittest`); `cryptography` in the control-plane image (only `app create` uses it, to make the deployment key); bash 3.2 for the launcher; Docker and Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "The admin API", "Bringing a deployment up", "Creating the App" and "Single instance, health, and deploys" (the facts `hosted status` shows). The 2a decisions this plan binds are in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. The ones it needs are copied under Global Constraints.

**Prerequisite:** 2a.1 through 2a.4b (PRs #18 to #22) are built. This plan changes 2a.1's `github_app.py` and 2a.4b's `control.py` and `supervisor.py`, each in a diff below.

## Global Constraints

- **Standard library only** outside the image, and **Python 3.9 syntax** (overview decision 15). Flat modules, imported by plain name (overview decision 19). A module run as a script outside the supervisor, which sets `PYTHONPATH` for its children, puts `hosted/` and `reviewer/` on `sys.path` itself.
- **The founder's rulings of 2026-10-09:** hosted files are JSON (overview decision 11 extends this to the admin roles file), and the CLI half of a command runs inside the image, never on the host's Python.
- **Overview decision 8:** `cloudflare` and `workersai` profiles load in `config validate` and are refused by `hosted config apply` with "not served by the hosted gateway until phase 2a.6".
- **Overview decision 10 (spike S8):** the manifest cannot enable the device flow, so `app create`'s checklist carries "tick Enable Device Flow at `https://github.com/organizations/ORG/settings/apps/SLUG`", and `hosted login` reports GitHub's 400 as that box unticked.
- **Overview decision 12, as revised in this plan:** the admin port is not published on the host. The CLI reaches it over the deployment's Docker network, so workers can reach it too until 2c, and every call requires a token this App issued that resolves to a role.
- **Overview decision 14:** in 2a a page is a `PAGE:` line in the control plane's log and an entry in `hosted status`.
- The spec's admin API: "Every admin call presents the token; the control plane resolves it to a GitHub numeric user id and checks that against the role lists". "Logins are never matched, because a renamed account releases its login for someone else to claim." The roles are `operators` (status, profiles, doctor, kindex pushes), `transcript_readers` and `erasers`. "There is no code to intercept and no admin call that works without a role."
- The spec's bringing-up order: `hosted deploy`, then `app create`, then the provider credentials, then `hosted deploy --admins ADMINS`, then the device-flow box, then `hosted login` and `hosted config apply FILE`. "Admins are platform configuration, not deployment configuration": only `hosted deploy --admins` changes them, and "`config apply` cannot". "`hosted login` prints the numeric id it resolved". "`hosted deploy` also reports the control plane's state".
- The spec's App creation: a one-shot loopback server, a random `state`, "accepts exactly one request whose path and `state` match, exchanges the code itself, at once, and exits", and "the manifest code is the credential". "For `--target local` the manifest leaves the webhook inactive and the poll does the work." The permissions table and the event list are the spec's, verbatim.
- 2a.4b's decision 17: a host reboot does not bring the control plane back by itself, and `hosted deploy` starts it. 2a.4b's decision 19: compose discovers no JSON filename, so every compose command names `hosted/compose.json`.
- Every existing suite (`test-python.sh`, `test-providers.sh`, `test-personas.sh`, `test-shim.sh`, `test-launcher.sh`, `test-worker.sh`) still passes, and local mode's prompts are byte-identical (`tests/fixtures/`).

## Decided here

The spec and the overview leave these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **The admin API is its own HTTP server in the control-plane process**, on port 8082 (`ADMIN_LISTEN`). It starts with `/healthz`, before the lock and before the configuration, and answers 503 with the control plane's state until there is a control plane to ask, so a call made too early hears "waiting for lock" or "not configured: ...", with the command that clears it. Both of its role-gated calls in 2a read the control plane's memory (`status`) or write its records store (`config apply`), which only that process can do.
2. **The admin port is not published on the host.** `claudebox.sh` runs the CLI on the deployment's `claudebox` network, where the admin API is `http://claudebox:8082`. Reaching it therefore takes Docker access on the host, which is platform access. The spec's "loopback on the host" would also let any local user connect. Workers share that network until 2c (overview decision 12, revised here to say so).
3. **A user token counts only when this App issued it.** The admin API asks GitHub's check-token call (`POST /applications/{client_id}/token`, authenticated as the App's OAuth client), which answers 404 for any other token and names the numeric user id for this one. 2a.1's `user_for_token` called `GET /user`, which every token the user holds would pass, the `gh` CLI's included. It is replaced by `user_for_app_token`. A user who revokes the App's authorization on GitHub ends every one of their tokens at once. The check needs the App's client id and secret, so `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET` join 2a.4b's closed list of control-plane secrets. A token is asked about at most once a minute (a digest is the cache key, and a refusal is cached as well as a success), and a token GitHub cannot be asked about is refused with 503, never let through.
4. **The roles are `CLAUDEBOX_ADMINS`**, one compact JSON line that `hosted deploy --admins` writes into `hosted/claudebox.env` from the ADMINS file. The file is a JSON object with the three roles as keys, each a list of numeric GitHub user ids. A login where an id belongs, a duplicate, an unknown role, a non-positive id, or a file with no operator is refused, every problem at once, and nothing is written. The control plane reads the line once, at startup, and the supervisor forwards it to the control plane alone. A line that is unset or does not parse is named in the log and leaves every role-gated call refused with that reason. 2a's two role-gated calls both need `operators`. `transcript_readers` and `erasers` are accepted now, so the file keeps its shape in 2b, and they grant nothing until their calls arrive.
5. **2a serves four admin calls**, under `/admin/v1`: `GET /app` (no token: the App's client id, slug, owner and settings page, which `hosted login` needs before it holds a token), `GET /whoami` (any token this App issued: the id, the login and the roles, empty for an unlisted id, so `hosted login` can print the id an operator has to list), `GET /status` and `PUT /config` (operators). A refusal names the id and the role it lacks, and the control plane logs each refusal. Nothing logs a token.
6. **`config apply` checks the file as `config validate --deploy` does**, refuses the providers 2a's gateway does not serve, and stores it with an audit row (`config_apply`, the login, the body's SHA-256 and the profile names). A profile whose credential the supervisor did not hand the control plane is applied with a warning that names the fix, because only a restart with the list can serve it. The control plane loads the new file before the call returns, and the next poll dispatches under it. `applied_at` always moves, even for two applies in one second, because the control plane reloads exactly when it moves. The loader runs under a lock, since the poll thread reloads too. A body over the deployment file's 256 KiB cap is refused unread.
7. **`hosted status` shows what waits on a person**, and no body, token, prompt or transcript. It covers the control plane's state and reason, the applied deployment (when, by whom, which profiles), the repos being reviewed, the running jobs and their pairs, parked pairs and profiles, repos deferred in a row, the outbox depth and oldest item per installation, withheld findings, and the newest pages. A withheld finding is named by repo, PR, persona and detector only. Its redacted body is a withheld-finding record, which is the `transcript_readers`' to read (2b's `withheld`). The pages are the last 100 `PAGE:` lines, which the control plane keeps by wrapping its own log function, so nothing that pages had to change. The gateway's one page of its own, a response without usage, commits as `capped` and pages again from the commit point. The supervisor's page stops the container.
8. **The CLI half runs in a throwaway container of the control-plane image**, with `--entrypoint python3`, since that image's entrypoint is the supervisor, which would otherwise start a deployment. The container runs as uid 65534 with no capabilities and `no-new-privileges`. Each command gets the network it needs and no more: none for checking ADMINS and the credential list, Docker's default for `whois` and `app create` (GitHub), and `claudebox` for `login`, `status` and `config apply`. The container prints on stdout only what the launcher keeps.
9. **`hosted deploy --target local` is safe to re-run.** It creates `hosted/claudebox.env` (mode 600) when it is missing, builds both images (`compose --profile build build`), and with `--admins` checks the file offline and writes `CLAUDEBOX_ADMINS`. It then rewrites `CLAUDEBOX_CREDENTIALS` from the env file's own names: every name the supervisor does not own, apart from Voyage's, which crosses by its fixed name. Only the names cross into the container, never a value. This revises 2a.4b's decision 15, which had the list come from the deployment file, a file the launcher does not have. It runs `compose up -d`, which recreates the container only when the image or the env file changed, so a re-run with nothing new costs no restart (each restart re-reviews every open PR, overview decision 1). Last, it reports `/healthz`, waiting up to a minute while the answer is "waiting for lock". `--target fly` and `--target gcp` are refused with the phase that brings each.
10. **`app create --org ORG --target local` runs the manifest flow in the CLI container**, which listens on 8765 behind `-p 127.0.0.1::8765`, so Docker chooses the host port and binds it on loopback only. The launcher reads the port with `docker port` and opens the browser on it. The start page builds the redirect URL from the first request's `Host` header, the only place the published port is visible from inside. It accepts only a loopback host, and only that one from then on. The callback must carry the flow's state, compared in constant time, and the code is exchanged once, since it is single-use. The App's secrets go from the container's stdout straight to the end of the env file. A PEM's newlines are written as `\n`, which 2a.4b's `pem_from_env` reads back. A deployment key already in the file is kept, because it encrypts the blob store. An env file already holding an App is refused, since a second App would orphan the first. The flow gives up after 15 minutes. A container on Docker's default bridge network can reach the flow's port, and the state makes any request it forges useless: the worst it can do is break a run, which is run again.
11. **`hosted login` is the device flow for the App's user OAuth**, with the client id from `GET /admin/v1/app`. That call reads the App's slug and owner with its JWT, once, to name the settings page. It obeys the flow's interval and `slow_down`, ends on `expired_token` and `access_denied`, and then calls `whoami` and prints the numeric id and the roles. The token is cached in `~/.config/claudebox/hosted-local.token` at mode 600. The launcher writes the file from the CLI's stdout and passes the token to later calls by name (`-e CLAUDEBOX_USER_TOKEN`), so it never sits in an argv. A token GitHub has expired or revoked is refused with "run hosted login". 2a refreshes nothing.
12. **`hosted whois LOGIN`** prints the id from GitHub's public user API and needs no credential. **`hosted logs`** follows the control plane's log, and **`hosted stop`** is `compose down`, which keeps the volume. These are the `logs` and `down` 2a.4b's decision 19 promised.
13. **Phase 1's F7:** `config validate` refuses every option it does not take and anything after `--`. `--repo` is refused by name, with `--repo-name OWNER/NAME` as the likely intent. Each hosted command does the same with its own options. Phase 1's pinned forms still pass: `--no-repo` beside `config validate` is accepted, and `--org`, `--deploy` and `--repo-name` beside `run` are refused with Phase 1's words.

## Review Focus

- **A token from another app, for a listed admin.** Expected: 401, because GitHub's check-token call does not know it as this App's. Pinned in Tasks 1 and 2.
- **A renamed account, and someone who claims the old login.** Expected: the renamed admin keeps the role, and the claimant gets none. Pinned in Task 2.
- **GitHub unreachable during a call.** Expected: 503, and the call does nothing. Pinned in Task 2.
- **A roles file that does not parse.** Expected: nothing is written, and an already-running control plane keeps its roles. A control plane started with a bad line refuses every role-gated call. Pinned in Tasks 2, 3 and 5.
- **Two `config apply` in one second.** Expected: both take effect. Pinned in Task 3.
- **`hosted status` leaking a withheld body, a prompt or a credential.** Expected: none of them appears in the snapshot. Pinned in Task 3.
- **A forged manifest callback, or a start page on a non-loopback host.** Expected: refused, and the flow keeps waiting for the real one. Pinned in Task 4.
- **The user token or an App secret on a command line or in the terminal.** Expected: neither. The token crosses by name, and the secrets go from stdout to the env file. Pinned in Task 5.

## Not in this plan

- **The other admin commands** (`profiles`, `doctor`, `kindex push`, `erase`, `withheld`, transcript reads) and the roles that use them. These are 2b's and later.
- **Refreshing an expired user token.** 2a asks for `hosted login` again.
- **Two deployments on one host.** The compose project, the network and the token file each have one fixed name.
- **Webhooks.** The manifest leaves the webhook inactive until phase 4.

## Not run here

The unit and launcher suites use fakes and `--dry-run`. Task 6 is the live run, which a person performs with Docker and a throwaway GitHub org, and which this plan's Validator records as evidence. It is also where three checks the earlier sub-plans deferred to 2a.5 happen: a check run written to GitHub (2a.4a), the worker channel serving a real worker (2a.4a), and a real `claude` resuming a transcript another container wrote (2a.3). It checks the GitHub behaviors this plan relies on and could not test offline: a manifest with an inactive webhook is accepted, the check-token call answers for a GitHub App's user tokens, and the device-flow endpoints answer 400 while the box is unticked.

---

### Task 1: GitHub calls for the operator surface

**Files:**
- Modify: `hosted/github_app.py`
- Test: `tests/test_hosted_github_app.py`

**Interfaces:**
- Consumes: 2a.1's `_Client`, `_Bare`, `App`, `Response`, `GitHubError`, `urllib_transport`.
- Produces:
  - `user_for_app_token(client_id, client_secret, user_token, transport=urllib_transport, api=API) -> Optional[dict]`, `{id, login}` or None. Replaces `user_for_token`.
  - `App.info() -> {slug, owner, client_id, html_url}`.
  - `convert_manifest(code, transport=urllib_transport, api=API) -> dict`.
  - `WEB = "https://github.com"`, `DeviceFlowDisabled`, `device_code(client_id, transport, web) -> dict`, `device_token(client_id, code, transport, web) -> dict`.

- [ ] **Step 1: Write the failing test**

Apply this change to `tests/test_hosted_github_app.py`:

```diff
diff --git a/tests/test_hosted_github_app.py b/tests/test_hosted_github_app.py
index 49a8454..6fa1cf2 100644
--- a/tests/test_hosted_github_app.py
+++ b/tests/test_hosted_github_app.py
@@ -4,8 +4,10 @@ import base64
 import json
 import unittest
 
-from github_app import (API, CHECK_RUN_NAME, READ_PERMISSIONS, SUMMARY_MAX, App, GitHubError,
-                        Response, app_jwt, is_fork_pr, is_public, parse_time, public_user, user_for_token)
+from github_app import (API, CHECK_RUN_NAME, READ_PERMISSIONS, SUMMARY_MAX, WEB, App,
+                        DeviceFlowDisabled, GitHubError, Response, app_jwt, convert_manifest,
+                        device_code, device_token, is_fork_pr, is_public, parse_time, public_user,
+                        user_for_app_token)
 
 
 class FakeSigner:
@@ -338,11 +340,91 @@ class ForkTest(unittest.TestCase):
 
 
 class UserTest(unittest.TestCase):
-    def test_user_for_token_resolves_the_numeric_id(self):
-        t = FakeTransport({("GET", "/user"): {"id": 42, "login": "octo"}})
-        self.assertEqual(user_for_token("gho_x", t), {"id": 42, "login": "octo"})
-        self.assertEqual(t.requests[0][2]["Authorization"], "Bearer gho_x")
+    def test_a_user_token_is_checked_as_this_apps_and_names_the_numeric_id(self):
+        t = FakeTransport({("POST", "/applications/Iv23abc/token"):
+                           {"user": {"id": 42, "login": "octo"}, "app": {"client_id": "Iv23abc"}}})
+        self.assertEqual(user_for_app_token("Iv23abc", "s3cret", "ghu_x", t),
+                         {"id": 42, "login": "octo"})
+        method, url, headers, body = t.requests[0]
+        self.assertEqual(headers["Authorization"],
+                         "Basic " + base64.b64encode(b"Iv23abc:s3cret").decode())
+        self.assertEqual(body, {"access_token": "ghu_x"})
+
+    def test_a_token_this_app_did_not_issue_is_nobody(self):
+        # GitHub answers 404 for a token the App's client did not issue: the
+        # gh CLI's token for the same admin administers nothing here.
+        for status in (404, 422):
+            with self.subTest(status=status):
+                t = FakeTransport({("POST", "/applications/Iv23abc/token"):
+                                   Response(status, {}, b'{"message": "Not Found"}')})
+                self.assertIsNone(user_for_app_token("Iv23abc", "s", "gho_other", t))
+
+    def test_github_unreachable_is_an_error_and_never_a_user(self):
+        t = FakeTransport({("POST", "/applications/Iv23abc/token"):
+                           Response(502, {}, b'{"message": "bad gateway"}')})
+        with self.assertRaises(GitHubError):
+            user_for_app_token("Iv23abc", "s", "ghu_x", t)
+
+    def test_a_client_id_that_could_alter_the_path_is_refused(self):
+        for bad in ("Iv23/../x", "", "abc", "Iv23?x=1"):
+            with self.subTest(bad=bad), self.assertRaises(ValueError):
+                user_for_app_token(bad, "s", "ghu_x", FakeTransport({}))
+
+    def test_the_app_describes_itself_with_its_jwt(self):
+        app, t, _ = app_with({("GET", "/app"): {"slug": "claudebox-acme", "owner": {"login": "acme"},
+                                                "client_id": "Iv23abc", "html_url": "https://x"}})
+        self.assertEqual(app.info(), {"slug": "claudebox-acme", "owner": "acme",
+                                      "client_id": "Iv23abc", "html_url": "https://x"})
+        self.assertTrue(t.requests[0][2]["Authorization"].startswith("Bearer "))
+
+    def test_a_manifest_code_is_converted_with_no_credential(self):
+        t = FakeTransport({("POST", "/app-manifests/abc_123/conversions"): {"id": 9, "pem": "k"}})
+        self.assertEqual(convert_manifest("abc_123", t)["id"], 9)
+        self.assertNotIn("Authorization", t.requests[0][2])
+        for bad in ("a/b", "", "a b"):
+            with self.subTest(bad=bad), self.assertRaises(ValueError):
+                convert_manifest(bad, FakeTransport({}))
+
+
+class DeviceFlowTest(unittest.TestCase):
+    class Web:
+        def __init__(self, answers):
+            self.answers, self.requests = list(answers), []
+
+        def __call__(self, method, url, headers, body):
+            self.requests.append((method, url, headers, body))
+            status, payload = self.answers.pop(0)
+            return Response(status, {}, json.dumps(payload).encode())
+
+    def test_the_device_code_is_asked_for_with_the_client_id_only(self):
+        web = self.Web([(200, {"device_code": "dc", "user_code": "ABCD-1234",
+                               "verification_uri": "https://github.com/login/device",
+                               "interval": 5, "expires_in": 900})])
+        self.assertEqual(device_code("Iv23abc", web)["user_code"], "ABCD-1234")
+        method, url, headers, body = web.requests[0]
+        self.assertEqual(url, f"{WEB}/login/device/code")
+        self.assertEqual(body, b"client_id=Iv23abc")
+        self.assertNotIn("Authorization", headers)
+
+    def test_a_pending_poll_is_an_answer_not_an_error(self):
+        web = self.Web([(200, {"error": "authorization_pending"})])
+        self.assertEqual(device_token("Iv23abc", "dc", web), {"error": "authorization_pending"})
+        self.assertIn(b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code",
+                      web.requests[0][3])
+
+    def test_an_unticked_device_flow_box_is_named(self):
+        # Overview decision 10: the manifest cannot enable the device flow,
+        # and until the box is ticked GitHub answers 400.
+        for answer in ((400, {}), (200, {"error": "device_flow_disabled"})):
+            with self.subTest(answer=answer), self.assertRaises(DeviceFlowDisabled):
+                device_code("Iv23abc", self.Web([answer]))
+
+    def test_any_other_refusal_is_a_github_error(self):
+        with self.assertRaises(GitHubError):
+            device_code("Iv23abc", self.Web([(503, {"message": "down"})]))
+
 
+class PublicUserTest(unittest.TestCase):
     def test_public_user_sends_no_credential(self):
         t = FakeTransport({("GET", "/users/octo"): {"id": 42, "login": "octo"}})
         self.assertEqual(public_user("octo", t)["id"], 42)
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_github_app.py'`
Expected: FAIL: `ImportError: cannot import name 'WEB' from 'github_app'`.

- [ ] **Step 3: Implement**

Apply this change to `hosted/github_app.py`:

```diff
diff --git a/hosted/github_app.py b/hosted/github_app.py
index 0cbd679..f28c5ab 100644
--- a/hosted/github_app.py
+++ b/hosted/github_app.py
@@ -225,6 +225,12 @@ class App(_Client):
     def installation(self, installation_id: int) -> "Installation":
         return Installation(self, installation_id)
 
+    def info(self) -> dict:
+        """The App itself: {slug, owner, client_id, html_url}. `hosted login` needs all four."""
+        data = self._call("GET", "/app")
+        return {"slug": data["slug"], "owner": (data.get("owner") or {}).get("login", ""),
+                "client_id": data["client_id"], "html_url": data.get("html_url", "")}
+
 
 class Installation(_Client):
     REFRESH_MARGIN = 300
@@ -376,11 +382,30 @@ class _Bare(_Client):
         return self._header
 
 
-def user_for_token(user_token: str, transport: Transport = urllib_transport,
-                   api: str = API) -> dict:
-    """{id, login} of the user a user token belongs to. What admin auth resolves."""
-    data = _Bare("Bearer " + user_token, transport, api)._call("GET", "/user")
-    return {"id": int(data["id"]), "login": data["login"]}
+def user_for_app_token(client_id: str, client_secret: str, user_token: str,
+                       transport: Transport = urllib_transport, api: str = API) -> Optional[dict]:
+    """{id, login} of the user a token belongs to, when this App issued it; else None.
+
+    What admin auth resolves. GitHub's check-token call is authenticated as
+    the App's OAuth client, and answers 404 for any token the App did not
+    issue: a token another app holds for an admin (the `gh` CLI's, say)
+    administers nothing here, and a user who revokes the App's authorization
+    ends every token at once.
+    """
+    if not re.fullmatch(r"Iv[0-9A-Za-z.]{1,64}", client_id or ""):
+        raise ValueError(f"not a GitHub App client id: {client_id!r}")
+    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
+    try:
+        data = _Bare("Basic " + basic, transport, api)._call(
+            "POST", f"/applications/{client_id}/token", {"access_token": user_token})
+    except GitHubError as e:
+        if e.status in (404, 422):
+            return None
+        raise
+    user = data.get("user") or {}
+    if not isinstance(user.get("id"), int):
+        return None
+    return {"id": int(user["id"]), "login": str(user.get("login", ""))}
 
 
 def public_user(login: str, transport: Transport = urllib_transport, api: str = API) -> dict:
@@ -389,3 +414,56 @@ def public_user(login: str, transport: Transport = urllib_transport, api: str =
         raise ValueError(f"not a GitHub login: {login!r}")
     data = _Bare("", transport, api)._call("GET", f"/users/{login}")
     return {"id": int(data["id"]), "login": data["login"]}
+
+
+def convert_manifest(code: str, transport: Transport = urllib_transport, api: str = API) -> dict:
+    """The App GitHub created from a manifest: `app create`'s one credential-bearing call.
+
+    The code is the credential (the conversion needs nothing else), so it is
+    exchanged once, at once, and never leaves the process that received it.
+    """
+    if not re.fullmatch(r"[0-9A-Za-z_-]{1,128}", code or ""):
+        raise ValueError("not a manifest code")
+    return _Bare("", transport, api)._call("POST", f"/app-manifests/{code}/conversions")
+
+
+WEB = "https://github.com"
+
+
+class DeviceFlowDisabled(Exception):
+    """The App's Enable Device Flow box is not ticked (overview decision 10)."""
+
+
+def _form(transport: Transport, url: str, fields: Mapping[str, str]) -> dict:
+    body = urllib.parse.urlencode(fields).encode()
+    resp = transport("POST", url, {"Accept": "application/json", "User-Agent": USER_AGENT,
+                                   "Content-Type": "application/x-www-form-urlencoded"}, body)
+    try:
+        data = json.loads(resp.body) if resp.body else {}
+    except ValueError:
+        data = {}
+    if not isinstance(data, dict):
+        data = {}
+    if data.get("error") == "device_flow_disabled":
+        raise DeviceFlowDisabled(data.get("error_description", ""))
+    if resp.status != 200:
+        # GitHub answers 400 here while the box is unticked; nothing else in
+        # this flow sends a request GitHub could refuse with a 400.
+        if resp.status == 400:
+            raise DeviceFlowDisabled(data.get("error_description", "") or f"HTTP {resp.status}")
+        raise GitHubError(resp.status, str(data.get("error_description") or data.get("message")
+                                           or "no message"))
+    return data
+
+
+def device_code(client_id: str, transport: Transport = urllib_transport, web: str = WEB) -> dict:
+    """{device_code, user_code, verification_uri, interval, expires_in}."""
+    return _form(transport, f"{web}/login/device/code", {"client_id": client_id})
+
+
+def device_token(client_id: str, code: str, transport: Transport = urllib_transport,
+                 web: str = WEB) -> dict:
+    """One poll: {access_token, ...} once granted, else {error: authorization_pending | ...}."""
+    return _form(transport, f"{web}/login/oauth/access_token",
+                 {"client_id": client_id, "device_code": code,
+                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_github_app.py'`, then `./test-python.sh`
Expected: PASS, 43 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/github_app.py tests/test_hosted_github_app.py
git commit -m "hosted: GitHub calls for the operator surface, and tokens only this App issued"
```

### Task 2: The admin API

**Files:**
- Create: `hosted/admin.py`
- Test: `tests/test_hosted_admin.py`

**Interfaces:**
- Consumes: Task 1's `user_for_app_token`; Phase 1's `config_load` (`read_json`, `Problems`, `is_int`, `ConfigError`); `deploy_config.DEPLOY_FILE_CAP`.
- Produces:
  - `ROLES = ("operators", "transcript_readers", "erasers")`, `ADMINS_VAR = "CLAUDEBOX_ADMINS"`, `ADMINS_CAP`, `PREFIX = "/admin/v1"`, `TOKEN_CACHE_SECONDS = 60`, `TOKEN_CACHE_MAX`, `ROLES_HINT`.
  - `AdminsError(ConfigError)`, `parse_admins(data: bytes) -> {role: frozenset[int]}`, `roles_of(roles, user_id) -> list`.
  - `TokenChecker(client_id, client_secret, check=user_for_app_token, clock=time.monotonic)` with `user(token) -> Optional[dict]` and the attribute `client_id`.
  - `Admin(status, app, checker, roles, roles_problem="", log=print)` with `handle(method, path, token, body) -> (status, payload)` and `app_info()`. `status` has `current` (the ControlPlane or None), `state` and `reason`. The ControlPlane gives it `status_snapshot()` and `apply_config(body, actor_id, actor_login)`, Task 3's.
  - `make_server(admin, host, port) -> ThreadingHTTPServer`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_admin.py`:

```python
import _path  # noqa: F401

import http.client
import json
import threading
import unittest

import admin
from admin import (ADMINS_CAP, Admin, AdminsError, TokenChecker, make_server, parse_admins,
                   roles_of)
from deploy_config import DEPLOY_FILE_CAP
from github_app import GitHubError

OPERATOR = {"id": 42, "login": "octo"}
STRANGER = {"id": 7, "login": "mallory"}
ROLES = {"operators": frozenset({42}), "transcript_readers": frozenset(), "erasers": frozenset({9})}


class ParseAdminsTest(unittest.TestCase):
    def test_numeric_ids_by_role(self):
        roles = parse_admins(b'{"operators": [42, 43], "erasers": [9]}')
        self.assertEqual(roles, {"operators": frozenset({42, 43}),
                                 "transcript_readers": frozenset(), "erasers": frozenset({9})})

    def test_a_login_where_an_id_belongs_names_whois(self):
        # Nothing resolves a login on the write path: a renamed account
        # releases its login for someone else to claim.
        with self.assertRaises(AdminsError) as cm:
            parse_admins(b'{"operators": ["octo"]}')
        self.assertIn("hosted whois LOGIN", str(cm.exception))

    def test_every_problem_is_named_at_once(self):
        with self.assertRaises(AdminsError) as cm:
            parse_admins(b'{"operator": [1], "erasers": [9, 9], "transcript_readers": [true]}')
        text = str(cm.exception)
        self.assertIn("did you mean 'operators'", text)
        self.assertIn("erasers: lists an id more than once", text)
        self.assertIn("transcript_readers: must be a list of numeric GitHub user ids", text)
        self.assertIn("operators: must list at least one id", text)

    def test_ids_must_be_positive(self):
        for bad in (b'{"operators": [0]}', b'{"operators": [-4]}', b'{"operators": 42}'):
            with self.subTest(bad=bad), self.assertRaises(AdminsError):
                parse_admins(bad)

    def test_a_file_with_no_operator_is_refused(self):
        with self.assertRaises(AdminsError) as cm:
            parse_admins(b'{"erasers": [9]}')
        self.assertIn("nobody can apply a deployment config", str(cm.exception))

    def test_the_strict_reader_applies(self):
        for bad in (b"not json", b'{"operators": [1], "operators": [2]}', b"[1]",
                    b"x" * (ADMINS_CAP + 1)):
            with self.subTest(bad=bad[:20]), self.assertRaises(AdminsError):
                parse_admins(bad)

    def test_roles_of_reads_ids_only(self):
        self.assertEqual(roles_of(ROLES, 42), ["operators"])
        self.assertEqual(roles_of(ROLES, 7), [])


class TokenCheckerTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.now = [], [100.0]
        self.answers = {"ghu_good": OPERATOR, "gho_other": None}

        def check(client_id, secret, token):
            self.calls.append((client_id, secret, token))
            answer = self.answers[token]
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.checker = TokenChecker("Iv23abc", "s3cret", check=check, clock=lambda: self.now[0])

    def test_github_is_asked_once_a_minute_per_token(self):
        self.assertEqual(self.checker.user("ghu_good"), OPERATOR)
        self.now[0] += 59
        self.assertEqual(self.checker.user("ghu_good"), OPERATOR)
        self.assertEqual(len(self.calls), 1)
        self.now[0] += 2
        self.checker.user("ghu_good")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], ("Iv23abc", "s3cret", "ghu_good"))

    def test_a_refusal_is_cached_too(self):
        self.assertIsNone(self.checker.user("gho_other"))
        self.assertIsNone(self.checker.user("gho_other"))
        self.assertEqual(len(self.calls), 1)

    def test_github_down_raises_and_caches_nothing(self):
        self.answers["ghu_good"] = GitHubError(0, "cannot reach GitHub")
        with self.assertRaises(GitHubError):
            self.checker.user("ghu_good")
        self.answers["ghu_good"] = OPERATOR
        self.assertEqual(self.checker.user("ghu_good"), OPERATOR)

    def test_only_a_digest_of_the_token_is_kept(self):
        self.checker.user("ghu_good")
        self.assertNotIn("ghu_good", repr(self.checker._cache))

    def test_the_cache_is_bounded(self):
        old = admin.TOKEN_CACHE_MAX
        admin.TOKEN_CACHE_MAX = 3
        self.addCleanup(setattr, admin, "TOKEN_CACHE_MAX", old)
        for i in range(10):
            self.answers[f"t{i}"] = None
            self.checker.user(f"t{i}")
        self.assertLessEqual(len(self.checker._cache), 3)


class FakeChecker:
    client_id = "Iv23abc"

    def __init__(self, users):
        self.users = users

    def user(self, token):
        answer = self.users.get(token)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeControl:
    def __init__(self):
        self.applied = []

    def status_snapshot(self):
        return {"state": "serving"}

    def apply_config(self, body, actor_id, login):
        self.applied.append((body, actor_id, login))
        return 200, {"applied_at": 1}


class Status:
    def __init__(self, current=None, state="waiting for lock", reason=""):
        self.current, self.state, self.reason = current, state, reason


class FakeApp:
    def __init__(self):
        self.calls = 0

    def info(self):
        self.calls += 1
        return {"slug": "claudebox-acme", "owner": "acme", "client_id": "Iv23abc", "html_url": "x"}


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.logs = []
        self.checker = FakeChecker({"op": OPERATOR, "stranger": STRANGER,
                                    "down": GitHubError(502, "bad gateway")})
        self.app = FakeApp()
        self.admin = Admin(Status(self.control, "serving"), self.app, self.checker, ROLES,
                           log=self.logs.append)

    def call(self, method, path, token="", body=b"", who=None):
        return (who or self.admin).handle(method, "/admin/v1" + path, token, body)

    def test_app_needs_no_token_and_names_the_settings_page(self):
        status, body = self.call("GET", "/app")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"client_id": "Iv23abc", "slug": "claudebox-acme", "owner": "acme",
                                "settings_url": "https://github.com/organizations/acme/settings/apps/"
                                                "claudebox-acme"})
        self.call("GET", "/app")
        self.assertEqual(self.app.calls, 1)

    def test_app_before_app_create_says_so(self):
        bare = Admin(Status(), None, None, None, "unset")
        status, body = self.call("GET", "/app", who=bare)
        self.assertEqual(status, 503)
        self.assertIn("app create", body["error"])

    def test_every_other_call_needs_a_token_this_app_issued(self):
        for method, path in (("GET", "/whoami"), ("GET", "/status"), ("PUT", "/config")):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path)[0], 401)
                status, body = self.call(method, path, "someone-elses")
                self.assertEqual(status, 401)
                self.assertIn("hosted login", body["error"])

    def test_an_unchecked_token_is_never_let_through(self):
        status, body = self.call("GET", "/status", "down")
        self.assertEqual(status, 503)
        self.assertIn("cannot check the token", body["error"])

    def test_whoami_answers_with_or_without_a_role(self):
        self.assertEqual(self.call("GET", "/whoami", "op"),
                         (200, {"id": 42, "login": "octo", "roles": ["operators"], "roles_problem": ""}))
        status, body = self.call("GET", "/whoami", "stranger")
        self.assertEqual((status, body["roles"]), (200, []))

    def test_role_gated_calls_refuse_an_id_with_no_role_and_log_it(self):
        for method, path in (("GET", "/status"), ("PUT", "/config")):
            with self.subTest(path=path):
                status, body = self.call(method, path, "stranger", b"{}")
                self.assertEqual(status, 403)
                self.assertIn("mallory (id 7) has no operators role", body["error"])
                self.assertIn("--admins", body["error"])
        self.assertEqual(self.control.applied, [])
        self.assertTrue(any("refused mallory (id 7)" in line for line in self.logs))

    def test_an_eraser_is_not_an_operator(self):
        checker = FakeChecker({"e": {"id": 9, "login": "eve"}})
        who = Admin(Status(self.control, "serving"), self.app, checker, ROLES, log=self.logs.append)
        self.assertEqual(self.call("GET", "/status", "e", who=who)[0], 403)

    def test_the_id_decides_and_never_the_login(self):
        # A renamed operator keeps the role; someone who claims the old login does not.
        checker = FakeChecker({"renamed": {"id": 42, "login": "octo-new"},
                               "claimant": {"id": 99, "login": "octo"}})
        who = Admin(Status(self.control, "serving"), self.app, checker, ROLES, log=self.logs.append)
        self.assertEqual(self.call("GET", "/status", "renamed", who=who)[0], 200)
        self.assertEqual(self.call("GET", "/status", "claimant", who=who)[0], 403)

    def test_unparsed_roles_refuse_everyone_with_the_reason(self):
        who = Admin(Status(self.control, "serving"), self.app, self.checker, None,
                    "CLAUDEBOX_ADMINS: admins file: operators: must list at least one id")
        status, body = self.call("GET", "/status", "op", who=who)
        self.assertEqual(status, 503)
        self.assertIn("must list at least one id", body["error"])
        self.assertEqual(self.call("GET", "/whoami", "op", who=who)[1]["roles"], [])

    def test_no_client_credentials_refuses_with_the_fix(self):
        who = Admin(Status(self.control, "serving"), self.app, None, ROLES)
        status, body = self.call("GET", "/whoami", "op", who=who)
        self.assertEqual(status, 503)
        self.assertIn("app create", body["error"])

    def test_before_the_lock_an_operator_hears_the_state(self):
        who = Admin(Status(None, "not configured", "no deployment config"), self.app,
                    self.checker, ROLES)
        status, body = self.call("GET", "/status", "op", who=who)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "the control plane is not configured: no deployment config")

    def test_status_and_config_reach_the_control_plane(self):
        self.assertEqual(self.call("GET", "/status", "op"), (200, {"state": "serving"}))
        self.assertEqual(self.call("PUT", "/config", "op", b'{"x": 1}')[0], 200)
        self.assertEqual(self.control.applied, [(b'{"x": 1}', 42, "octo")])

    def test_an_unknown_call_or_method_is_404(self):
        for method, path in (("GET", "/config"), ("PUT", "/status"), ("GET", "/withheld"),
                             ("POST", "/app")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, "op")[0], 404)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.admin = Admin(Status(self.control, "serving"), FakeApp(),
                           FakeChecker({"op": OPERATOR}), ROLES, log=lambda line: None)
        self.server = make_server(self.admin, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, token=None, body=None, length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if length is not None:
            headers["Content-Length"] = str(length)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_the_bearer_token_reaches_the_check(self):
        self.assertEqual(self.request("GET", "/admin/v1/whoami", "op")[1]["id"], 42)
        self.assertEqual(self.request("GET", "/admin/v1/whoami?x=1", "op")[0], 200)
        self.assertEqual(self.request("GET", "/admin/v1/whoami")[0], 401)

    def test_a_body_is_read_for_put_and_capped_at_the_deploy_file_cap(self):
        self.assertEqual(self.request("PUT", "/admin/v1/config", "op", b'{"a": 1}')[0], 200)
        self.assertEqual(self.control.applied[0][0], b'{"a": 1}')
        status, body = self.request("PUT", "/admin/v1/config", "op", b"", length=DEPLOY_FILE_CAP + 1)
        self.assertEqual(status, 413)
        self.assertEqual(len(self.control.applied), 1)

    def test_a_call_that_raises_is_a_500_and_the_server_lives(self):
        def boom():
            raise RuntimeError("no")
        self.control.status_snapshot = boom
        self.assertEqual(self.request("GET", "/admin/v1/status", "op")[0], 500)
        self.assertEqual(self.request("GET", "/admin/v1/app")[0], 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_admin.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'admin'`.

- [ ] **Step 3: Implement**

`hosted/admin.py`:

```python
"""The admin API: GitHub user tokens, numeric-id roles, `status` and `config apply`.

The spec's "The admin API". Every call but `app` presents a GitHub user token
in `Authorization: Bearer`. The control plane asks GitHub whether this App
issued it (`github_app.user_for_app_token`), which also names the numeric user
id, and checks that id against the role lists the platform hands it in
`CLAUDEBOX_ADMINS`. Logins are never matched: a renamed account releases its
login for someone else to claim. A token the App did not issue, one GitHub
cannot be asked about, and an id with no role are all refused, and nothing
here works without a role but `whoami`, which answers "no role" so that
`hosted login` can print the id an operator has to list.

The roles are platform configuration: only `hosted deploy --admins` writes
them, and this process reads them once, at startup. A role list that does not
parse leaves every role-gated call refused, with the reason, rather than
falling back to anyone.

2a serves four calls under /admin/v1:

    GET  /admin/v1/app       no token: the App's client id and settings page, for `hosted login`
    GET  /admin/v1/whoami    any token this App issued: {id, login, roles}
    GET  /admin/v1/status    operators: the deployment's state, for `hosted status`
    PUT  /admin/v1/config    operators: the body is a deployment file; `hosted config apply`

The server starts with the process, before the lock or the configuration, and
answers 503 with the reason until the control plane can serve a call.
"""

import hashlib
import http.server
import json
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from config_load import ConfigError, Problems, is_int, read_json
from deploy_config import DEPLOY_FILE_CAP
from github_app import GitHubError, user_for_app_token

ROLES = ("operators", "transcript_readers", "erasers")
ADMINS_VAR = "CLAUDEBOX_ADMINS"
ADMINS_CAP = 64 * 1024
PREFIX = "/admin/v1"
# A token GitHub vouched for (or refused) is not asked about again for a
# minute, which bounds the check to one GitHub call per token per minute.
TOKEN_CACHE_SECONDS = 60
TOKEN_CACHE_MAX = 1000
LOGIN_HINT = "run `claudebox.sh hosted login`"
ROLES_HINT = "list the id in ADMINS and run `claudebox.sh hosted deploy --target local --admins ADMINS`"

Roles = Dict[str, FrozenSet[int]]


class AdminsError(ConfigError):
    pass


def parse_admins(data: bytes) -> Roles:
    """The role lists from an ADMINS file: numeric GitHub user ids, and only ids.

    Raises AdminsError naming every problem. A login where an id belongs is
    refused with the command that finds the id, since nothing resolves a
    login on the write path. A file with no operator is refused too: nobody
    could apply a deployment config under it.
    """
    try:
        raw = read_json(data, ADMINS_CAP, "admins file")
    except ConfigError as exc:
        raise AdminsError(str(exc)) from None
    problems = Problems()
    roles: Dict[str, FrozenSet[int]] = {}
    malformed = set()
    for key, value in raw.items():
        if key not in ROLES:
            problems.unknown("", key, ROLES)
            continue
        if not isinstance(value, list) or not all(is_int(v) and v >= 1 for v in value):
            problems.add(key, "must be a list of numeric GitHub user ids; "
                              "`claudebox.sh hosted whois LOGIN` prints a login's id")
            malformed.add(key)
            continue
        if len(set(value)) != len(value):
            problems.add(key, "lists an id more than once")
        roles[key] = frozenset(value)
    if not roles.get("operators") and "operators" not in malformed:
        problems.add("operators", "must list at least one id; with none, nobody can apply a "
                                  "deployment config")
    if problems:
        raise AdminsError("; ".join(f"admins file: {line}" for line in problems.lines()))
    return {r: roles.get(r, frozenset()) for r in ROLES}


def roles_of(roles: Roles, user_id: int) -> list:
    return [r for r in ROLES if user_id in roles.get(r, ())]


class TokenChecker:
    """Who a user token belongs to, asked of GitHub at most once a minute per token.

    Only a digest of the token is kept. A refusal is cached as well as a
    success, so a stale token retried in a loop costs GitHub one call a
    minute. A GitHub that cannot be asked raises GitHubError, which the
    caller turns into a refusal: an unchecked token is never let through.
    """

    def __init__(self, client_id: str, client_secret: str, check=user_for_app_token,
                 clock: Callable[[], float] = time.monotonic):
        self.client_id, self.client_secret, self.check, self.clock = (
            client_id, client_secret, check, clock)
        self._lock = threading.Lock()
        self._cache: Dict[bytes, Tuple[float, Optional[dict]]] = {}

    def user(self, token: str) -> Optional[dict]:
        key = hashlib.sha256(token.encode("utf-8", "replace")).digest()
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < TOKEN_CACHE_SECONDS:
                return hit[1]
        user = self.check(self.client_id, self.client_secret, token)
        with self._lock:
            if len(self._cache) >= TOKEN_CACHE_MAX:
                self._cache = {k: v for k, v in self._cache.items()
                               if now - v[0] < TOKEN_CACHE_SECONDS}
                if len(self._cache) >= TOKEN_CACHE_MAX:
                    self._cache.clear()
            self._cache[key] = (now, user)
        return user


class Admin:
    """The four calls, behind the token check and the role lists.

    `status` is the process's _Status: its `current` is the ControlPlane once
    one holds the lock, and None before. `app` is None while the deployment
    has no App credentials, and `checker` None while it has no client
    credentials. `roles` is None, with `roles_problem` saying why, when
    CLAUDEBOX_ADMINS is unset or does not parse.
    """

    def __init__(self, status, app, checker: Optional[TokenChecker], roles: Optional[Roles],
                 roles_problem: str = "", log: Callable[[str], None] = lambda line: print(line, flush=True)):
        self.status, self.app, self.checker = status, app, checker
        self.roles, self.roles_problem, self.log = roles, roles_problem, log
        self._info: Optional[dict] = None

    # -- the token and the role --------------------------------------------------
    def _who(self, token: str) -> Tuple[Optional[dict], Optional[Tuple[int, Dict[str, Any]]]]:
        if self.checker is None:
            return None, (503, {"error": "the deployment has no App client credentials yet; "
                                         "`claudebox.sh app create` writes them"})
        if not token:
            return None, (401, {"error": f"no token; {LOGIN_HINT}"})
        try:
            user = self.checker.user(token)
        except GitHubError as exc:
            return None, (503, {"error": f"cannot check the token with GitHub: {exc}"})
        if user is None:
            return None, (401, {"error": "GitHub does not know this token as one this App "
                                         f"issued (expired, revoked, or another app's); {LOGIN_HINT}"})
        return user, None

    def _allowed(self, user: dict, role: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        if self.roles is None:
            return 503, {"error": f"no admin roles are configured: {self.roles_problem}"}
        if user["id"] in self.roles.get(role, ()):
            return None
        self.log(f"admin: refused {user['login']} (id {user['id']}): no {role} role")
        return 403, {"error": f"{user['login']} (id {user['id']}) has no {role} role; {ROLES_HINT}"}

    def _control(self) -> Tuple[Any, Optional[Tuple[int, Dict[str, Any]]]]:
        control = getattr(self.status, "current", None)
        if control is None:
            reason = getattr(self.status, "reason", "")
            return None, (503, {"error": f"the control plane is {self.status.state}"
                                         + (f": {reason}" if reason else "")})
        return control, None

    # -- the calls ------------------------------------------------------------------
    def handle(self, method: str, path: str, token: str, body: bytes) -> Tuple[int, Dict[str, Any]]:
        route = (method, path)
        if route == ("GET", PREFIX + "/app"):
            return self.app_info()
        if route not in (("GET", PREFIX + "/whoami"), ("GET", PREFIX + "/status"),
                         ("PUT", PREFIX + "/config")):
            return 404, {"error": f"no such call: {method} {path}"}
        user, refused = self._who(token)
        if refused is not None:
            return refused
        if route == ("GET", PREFIX + "/whoami"):
            return 200, {"id": user["id"], "login": user["login"],
                         "roles": roles_of(self.roles or {}, user["id"]),
                         "roles_problem": self.roles_problem if self.roles is None else ""}
        refused = self._allowed(user, "operators")
        if refused is not None:
            return refused
        control, refused = self._control()
        if refused is not None:
            return refused
        if route == ("GET", PREFIX + "/status"):
            return 200, control.status_snapshot()
        return control.apply_config(body, user["id"], user["login"])

    def app_info(self) -> Tuple[int, Dict[str, Any]]:
        if self.app is None or self.checker is None:
            return 503, {"error": "the deployment has no App yet; run `claudebox.sh app create`"}
        if self._info is None:
            try:
                info = self.app.info()
            except GitHubError as exc:
                return 502, {"error": f"cannot read the App from GitHub: {exc}"}
            owner_page = (f"https://github.com/organizations/{info['owner']}/settings/apps/"
                          f"{info['slug']}") if info.get("owner") else ""
            self._info = {"client_id": self.checker.client_id, "slug": info["slug"],
                          "owner": info.get("owner", ""), "settings_url": owner_page}
        return 200, dict(self._info)


class Handler(http.server.BaseHTTPRequestHandler):
    admin: Admin = None   # set on the subclass make_server builds
    timeout = 30

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        body = b""
        if method == "PUT":
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send(411, {"error": "Content-Length is required"})
            if length < 0 or length > DEPLOY_FILE_CAP:
                self.close_connection = True
                return self._send(413, {"error": f"the body is over {DEPLOY_FILE_CAP} bytes, "
                                                 f"the deployment file's cap"})
            body = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        try:
            status, payload = self.admin.handle(method, path, token, body)
        except Exception as exc:   # one call must not take the server with it
            self.admin.log(f"WARN: admin call {method} {path} failed: {type(exc).__name__}: {exc}")
            status, payload = 500, {"error": "the call failed; the control plane's log says why"}
        self._send(status, payload)

    def do_GET(self):
        self._serve("GET")

    def do_PUT(self):
        self._serve("PUT")


def make_server(admin: Admin, host: str, port: int) -> http.server.ThreadingHTTPServer:
    handler = type("BoundAdminHandler", (Handler,), {"admin": admin})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_admin.py'`, then `./test-python.sh`
Expected: PASS, 28 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/admin.py tests/test_hosted_admin.py
git commit -m "hosted: the admin API, numeric-id roles over tokens this App issued"
```

### Task 3: The control plane serves it

**Files:**
- Modify: `hosted/control.py`, `hosted/supervisor.py`
- Test: `tests/test_hosted_control.py`, `tests/test_hosted_supervisor.py`

**Interfaces:**
- Consumes: Task 2's `admin` module; 2a.4b's `ControlPlane`, `_Status`, `main`, `split_env`.
- Produces:
  - In `control.py`: `NOT_SERVED = ("cloudflare", "workersai")`, `PAGES_KEPT = 100`, `PageBook(log, clock)` (callable, with `recent() -> [{at, page}]`), `ControlPlane.pages`, `ControlPlane.apply_config(body, actor_id, actor_login) -> (status, payload)`, `ControlPlane.status_snapshot() -> dict`, `admin_from_env(env, status, app, log=print) -> Admin`. `main` serves the admin API on `ADMIN_LISTEN` (default `0.0.0.0:8082`).
  - In `supervisor.py`: `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET` in `CONTROL_SECRETS`, and `ADMIN_LISTEN` and `CLAUDEBOX_ADMINS` in `CONTROL_SETTINGS`.

- [ ] **Step 1: Write the failing tests**

Apply this change to `tests/test_hosted_control.py` and `tests/test_hosted_supervisor.py`:

```diff
diff --git a/tests/test_hosted_control.py b/tests/test_hosted_control.py
index d794d4a..4d7a014 100644
--- a/tests/test_hosted_control.py
+++ b/tests/test_hosted_control.py
@@ -13,7 +13,8 @@ import creds
 import gwproto
 from blobs import Blobs
 from control import (DRAINING, NOT_CONFIGURED, SERVING, WAITING, ControlPlane, GatewayLink,
-                     health_server, keys_from_env, pem_from_env, _Status, LOCK_FILE)
+                     PageBook, admin_from_env, health_server, keys_from_env, pem_from_env, _Status,
+                     LOCK_FILE)
 from records import InstanceLock, Records
 
 ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
@@ -440,5 +441,159 @@ class GatewayLinkTest(unittest.TestCase):
         self.assertEqual(seen, ["flush", "hello", "flush"])
 
 
+class ApplyConfigTest(Base):
+    """`hosted config apply`, as the admin API calls it (2a.5)."""
+
+    def body(self, doc=DEPLOY):
+        return json.dumps(doc).encode()
+
+    def test_a_valid_file_is_stored_audited_and_loaded_at_once(self):
+        status, out = self.control.apply_config(self.body(), 42, "octo")
+        self.assertEqual(status, 200)
+        self.assertEqual((out["profiles"], out["warnings"], out["poll_seconds"]), (["ollama"], [], 60))
+        row = self.records.conn().execute(
+            "SELECT body, applied_at, applied_by FROM deploy_config WHERE id = 1").fetchone()
+        self.assertEqual((bytes(row[0]), row[1], row[2]), (self.body(), int(T), 42))
+        audit = self.records.conn().execute("SELECT actor_id, action, scope, detail FROM audit").fetchall()
+        self.assertEqual([(a[0], a[1], a[2]) for a in audit], [(42, "config_apply", "deployment")])
+        self.assertEqual(json.loads(audit[0][3])["login"], "octo")
+        # Loaded before the call returns, not at the next poll.
+        self.assertEqual(self.control.outbox.comment_bytes, 1234)
+
+    def test_two_applies_in_one_second_both_take_effect(self):
+        # deploy() reloads when applied_at moves; the clock here never moves.
+        self.control.apply_config(self.body(), 42, "octo")
+        doc = json.loads(json.dumps(DEPLOY))
+        doc["caps"]["comment_bytes"] = 777
+        status, out = self.control.apply_config(self.body(doc), 42, "octo")
+        self.assertEqual(out["applied_at"], int(T) + 1)
+        self.assertEqual(self.control.outbox.comment_bytes, 777)
+
+    def test_a_file_that_does_not_load_names_every_problem_and_stores_nothing(self):
+        status, out = self.control.apply_config(b'{"profiles": {}, "nope": 1}', 42, "octo")
+        self.assertEqual(status, 422)
+        self.assertTrue(any("nope" in p for p in out["problems"]))
+        self.assertTrue(any("profiles" in p for p in out["problems"]))
+        self.assertEqual(self.records.conn().execute("SELECT COUNT(*) FROM deploy_config").fetchone()[0], 0)
+        self.assertEqual(self.records.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
+        self.assertEqual(self.control.apply_config(b"not json", 42, "octo")[0], 422)
+
+    def test_the_providers_2a_does_not_serve_are_refused(self):
+        # Overview decision 8: they load in `config validate` and stop here.
+        doc = json.loads(json.dumps(DEPLOY))
+        doc["profiles"]["wai"] = {"provider": "workersai", "models": ["@cf/x/y"],
+                                  "default_model": "@cf/x/y", "credential": "CF_TOKEN",
+                                  "budget": {"unit": "tokens", "daily": 1}}
+        status, out = self.control.apply_config(self.body(doc), 42, "octo")
+        self.assertEqual(status, 422)
+        self.assertEqual(out["problems"], ["deploy file: profiles.wai: provider workersai is not "
+                                           "served by the hosted gateway until phase 2a.6"])
+        self.assertEqual(self.records.conn().execute("SELECT COUNT(*) FROM deploy_config").fetchone()[0], 0)
+
+    def test_a_credential_the_supervisor_did_not_hand_over_is_applied_with_the_fix(self):
+        doc = json.loads(json.dumps(DEPLOY))
+        doc["profiles"]["ollama"]["credential"] = "NEW_KEY"
+        status, out = self.control.apply_config(self.body(doc), 42, "octo")
+        self.assertEqual(status, 200)
+        self.assertEqual(len(out["warnings"]), 1)
+        self.assertIn("NEW_KEY", out["warnings"][0])
+        self.assertIn("hosted deploy --target local", out["warnings"][0])
+
+    def test_a_not_configured_control_plane_serves_on_the_next_poll(self):
+        self.control.take_lock()
+        self.assertEqual(self.control.state, NOT_CONFIGURED)
+        self.control.apply_config(self.body(), 42, "octo")
+        self.control.poll_once()
+        self.assertEqual(self.control.state, SERVING)
+
+
+class StatusSnapshotTest(Base):
+    def test_the_snapshot_names_what_waits_and_holds_no_body(self):
+        from channel import JobMaterial, PairMaterial
+        from commit import Park, _Profile
+        from common import Pair
+        self.apply()
+        self.control.take_lock()
+        self.control.scheduler.installation_of["acme/web"] = 7
+        self.control.jobs.add(JobMaterial(
+            "job-1", 7, "acme/web", 42, 12, "a" * 40, "code", "ollama", "glm", "ANTHROPIC_AUTH_TOKEN",
+            {}, 4, True, [PairMaterial("sage", 2, "sys", "p"), PairMaterial("red_team", 1, "sys", "p")],
+            "http://gw", "http://ch", completed={"red_team": "ok"}))
+        self.control.committer.parks[("acme/web", Pair(12, "code", "sage"))] = Park("capped")
+        self.control.committer.profiles["ollama"] = _Profile(parked_until=int(T) + 600)
+        self.control.committer.defers_in_a_row["acme/web"] = 2
+        with self.records.tx() as c:
+            c.execute("INSERT INTO outbox (key, installation_id, repo, pr, head, body, next_attempt_at, "
+                      "created_at) VALUES ('j/sage/1', 7, 'acme/web', 12, 'h', 'the body', 0, ?)",
+                      (int(T) - 90,))
+            for i, persona in enumerate(("sage", "sme")):
+                c.execute("INSERT INTO withheld (repo, pr, job_id, persona, detector, salted_hash, "
+                          "redacted_body, created_at) VALUES ('acme/web', 12, 'j', ?, 'aws-key', x'00', "
+                          "'SECRET-ISH REDACTED BODY', ?)", (persona, int(T) + i))
+        self.control.log("PAGE: acme/web deferred 3 times in a row, a claudebox fault")
+        self.control.log("not a page")
+        s = self.control.status_snapshot()
+        self.assertEqual((s["state"], s["deployment"]["profiles"]), (SERVING, ["ollama"]))
+        self.assertEqual(s["repos"], [{"repo": "acme/web", "installation_id": 7}])
+        self.assertEqual(s["jobs"][0]["personas"], ["sage", "red_team"])
+        self.assertEqual(s["jobs"][0]["head"], "a" * 12)
+        self.assertEqual(s["parked_pairs"], [{"repo": "acme/web", "pr": 12, "mode": "code",
+                                              "persona": "sage", "reason": "capped", "until": None}])
+        self.assertEqual(s["parked_profiles"], {"ollama": int(T) + 600})
+        self.assertEqual(s["deferred_in_a_row"], {"acme/web": 2})
+        self.assertEqual(s["outbox"], [{"installation_id": 7, "items": 1, "oldest_age": 90}])
+        self.assertEqual(s["withheld"], [{"repo": "acme/web", "count": 2, "pr": 12, "persona": "sme",
+                                          "detector": "aws-key", "at": int(T) + 1}])
+        self.assertEqual([p["page"] for p in s["pages"]],
+                         ["acme/web deferred 3 times in a row, a claudebox fault"])
+        text = json.dumps(s)
+        for leak in ("REDACTED BODY", "the body", "sys", "olk-a-long-secret"):
+            self.assertNotIn(leak, text)
+
+    def test_before_a_deployment_config_the_snapshot_says_so(self):
+        self.control.take_lock()
+        s = self.control.status_snapshot()
+        self.assertEqual((s["state"], s["deployment"]), (NOT_CONFIGURED, None))
+        self.assertIn("hosted config apply", s["reason"])
+
+
+class PageBookTest(unittest.TestCase):
+    def test_pages_are_kept_bounded_and_every_line_still_logs(self):
+        logged = []
+        book = PageBook(logged.append, lambda: 5)
+        for i in range(150):
+            book(f"PAGE: number {i}")
+        book("WARN: not a page")
+        self.assertEqual(len(logged), 151)
+        recent = book.recent()
+        self.assertEqual(len(recent), 100)
+        self.assertEqual(recent[-1], {"at": 5, "page": "number 149"})
+
+
+class AdminFromEnvTest(unittest.TestCase):
+    def build(self, env):
+        logs = []
+        return admin_from_env(env, _Status(), None, log=logs.append), logs
+
+    def test_roles_and_the_checker_come_from_the_platform(self):
+        a, logs = self.build({"GITHUB_APP_CLIENT_ID": "Iv23abc", "GITHUB_APP_CLIENT_SECRET": "s",
+                              "CLAUDEBOX_ADMINS": '{"operators":[42],"transcript_readers":[],"erasers":[]}'})
+        self.assertEqual(a.roles["operators"], frozenset({42}))
+        self.assertEqual(a.checker.client_id, "Iv23abc")
+        self.assertEqual(logs, [])
+
+    def test_unset_or_malformed_roles_are_named_and_refuse_everyone(self):
+        for raw, words in (("", "is not set"), ('{"operators": ["octo"]}', "numeric GitHub user ids")):
+            with self.subTest(raw=raw):
+                a, logs = self.build({"CLAUDEBOX_ADMINS": raw} if raw else {})
+                self.assertIsNone(a.roles)
+                self.assertIn(words, a.roles_problem)
+                self.assertTrue(logs[0].startswith("WARN: admin roles: "))
+
+    def test_no_client_credentials_is_no_checker(self):
+        a, _ = self.build({"GITHUB_APP_CLIENT_ID": "Iv23abc"})
+        self.assertIsNone(a.checker)
+
+
 if __name__ == "__main__":
     unittest.main()
diff --git a/tests/test_hosted_supervisor.py b/tests/test_hosted_supervisor.py
index 6eda14d..a6e237d 100644
--- a/tests/test_hosted_supervisor.py
+++ b/tests/test_hosted_supervisor.py
@@ -6,7 +6,7 @@ import tempfile
 import unittest
 import unittest.mock
 
-from supervisor import (CAP_KILL, Spec, Supervisor, docker_gid, drop_privileges, main,
+from supervisor import (CAP_KILL, Spec, Supervisor, credential_names, docker_gid, drop_privileges, main,
                         provider_secrets, specs, split_env)
 
 PLATFORM = {
@@ -69,6 +69,22 @@ class EnvSplitTest(unittest.TestCase):
             self.assertNotIn(name, envs["gateway"])
             self.assertNotIn("CLAUDEBOX_SCAN_" + name, envs["control"])
 
+    def test_the_admin_apis_secrets_and_roles_reach_the_control_plane_alone(self):
+        # 2a.5: the client credentials check a user token; the roles gate it.
+        env = dict(PLATFORM, GITHUB_APP_CLIENT_ID="Iv23abc", GITHUB_APP_CLIENT_SECRET="client-secret",
+                   CLAUDEBOX_ADMINS='{"operators":[42]}', ADMIN_LISTEN="0.0.0.0:9082")
+        envs = split_env(env, "g", "r")
+        for name in ("GITHUB_APP_CLIENT_ID", "GITHUB_APP_CLIENT_SECRET", "CLAUDEBOX_ADMINS",
+                     "ADMIN_LISTEN"):
+            self.assertEqual(envs["control"][name], env[name])
+            self.assertNotIn(name, envs["gateway"])
+            self.assertNotIn(name, envs["runnerd"])
+        self.assertNotIn("client-secret", set(envs["gateway"].values()) | set(envs["runnerd"].values()))
+        with self.assertRaises(ValueError):
+            credential_names({"CLAUDEBOX_CREDENTIALS": "GITHUB_APP_CLIENT_SECRET"})
+        with self.assertRaises(ValueError):
+            credential_names({"CLAUDEBOX_CREDENTIALS": "CLAUDEBOX_ADMINS"})
+
     def test_the_platform_cannot_set_what_the_supervisor_generates(self):
         # PR #22 review: these crossed as "credentials" and overrode the gateway's.
         env = dict(PLATFORM, GATEWAY_SECRET="pinned", CONTROL_SOCKET="/elsewhere",
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_hosted_control.py'`, then `./test-python.sh -p 'test_hosted_supervisor.py'`
Expected: FAIL: `ImportError: cannot import name 'PageBook' from 'control'`, and in the supervisor's tests `test_the_admin_apis_secrets_and_roles_reach_the_control_plane_alone` fails with `KeyError: 'GITHUB_APP_CLIENT_ID'`.

- [ ] **Step 3: Implement**

Apply this change to `hosted/control.py` and `hosted/supervisor.py`:

```diff
diff --git a/hosted/control.py b/hosted/control.py
index 8b8c1dd..6e31f76 100644
--- a/hosted/control.py
+++ b/hosted/control.py
@@ -13,23 +13,30 @@ nothing is dispatched.
 Then three loops run on threads: the poll (each installation's scheduler
 poll, then the reap of ended jobs), the outbox drain (one Drainer per
 installation, round-robin), and the gateway's report socket. The worker
-channel is its own HTTP server. On SIGTERM dispatching stops, running jobs
-get until the drain deadline, and whatever is left is ended as revoked.
+channel is its own HTTP server, and so is the admin API (admin.py), which
+starts with /healthz and answers 503 until there is a control plane to ask.
+On SIGTERM dispatching stops, running jobs get until the drain deadline, and
+whatever is left is ended as revoked.
 """
 
+import collections
+import hashlib
 import http.server
+import json
 import os
 import signal
 import threading
 import time
-from typing import Any, Callable, Dict, List, Mapping, Optional
+from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple
 
+import admin as admin_mod
 import blobs as blobs_mod
 import commit as commit_mod
 import gwproto
 from channel import Channel, Jobs, make_server as make_channel_server
 from checkruns import CheckRuns
 from commit import Committer
+from config_load import ConfigError
 from deploy_config import DeployError, load_deploy
 from github_app import GitHubError
 from outbox import Drainer, Outbox
@@ -48,6 +55,32 @@ NO_DEPLOY = ("no deployment config in the records store yet; apply one with "
              "`claudebox.sh hosted config apply FILE`")
 NO_KEYS = ("the App credentials or the deployment key are missing or malformed; "
            "run `claudebox.sh hosted deploy`, then `claudebox.sh app create`")
+# Overview decision 8: these load in `config validate` and are refused here.
+NOT_SERVED = ("cloudflare", "workersai")
+PAGES_KEPT = 100
+
+
+class PageBook:
+    """The recent PAGE: lines, for `hosted status` (overview decision 14).
+
+    It wraps the process's log function, so every page this process writes
+    (the scheduler's, the commit point's, the outbox's) is kept with its
+    time, and nothing has to be told to report one. The gateway's one page
+    of its own, a response without usage, commits as `capped` here and pages
+    again from the commit point; the supervisor's stops the container.
+    """
+
+    def __init__(self, log: Callable[[str], None], clock: Callable[[], float]):
+        self._log, self.clock = log, clock
+        self._pages: Deque[Tuple[int, str]] = collections.deque(maxlen=PAGES_KEPT)
+
+    def __call__(self, line: str) -> None:
+        if line.startswith("PAGE: "):
+            self._pages.append((int(self.clock()), line[len("PAGE: "):]))
+        self._log(line)
+
+    def recent(self) -> List[Dict[str, Any]]:
+        return [{"at": at, "page": text} for at, text in list(self._pages)]
 
 
 class GatewayLink:
@@ -85,11 +118,14 @@ class ControlPlane:
                  clock: Callable[[], float] = time.time,
                  log: Callable[[str], None] = lambda line: print(line, flush=True)):
         self.records, self.lock, self.creds, self.blobs = records, lock, creds, blobs
+        self.pages = PageBook(log, clock)
+        log = self.pages
         self.app, self.runner, self.clock, self.log = app, runner, clock, log
         self.state = WAITING
         self.stopping = threading.Event()
         self.threads: Dict[str, threading.Thread] = {}
         self._deploy, self._deploy_stamp = None, None
+        self._deploy_lock = threading.Lock()   # the poll and `config apply` both reload
         self._installations: Dict[int, Any] = {}
         self._drainers: Dict[int, Drainer] = {}
         # Every candidate literal the supervisor handed over. The scanner uses
@@ -134,6 +170,10 @@ class ControlPlane:
 
     def deploy(self):
         """The deployment config in the records store, re-read when `config apply` changes it."""
+        with self._deploy_lock:
+            return self._load_deploy()
+
+    def _load_deploy(self):
         row = self.records.conn().execute(
             "SELECT body, applied_at FROM deploy_config WHERE id = 1").fetchone()
         if row is None:
@@ -191,6 +231,96 @@ class ControlPlane:
                             counts=record.counts() if record else {}))
         return out
 
+    # -- the admin API's two calls ---------------------------------------------------
+    def apply_config(self, body: bytes, actor_id: int, actor_login: str) -> Tuple[int, Dict[str, Any]]:
+        """`hosted config apply`: validate, store, audit, and load at once.
+
+        The file is checked as `config validate --deploy` checks it, and the
+        providers 2a's gateway does not serve are refused. A profile whose
+        credential the supervisor did not hand over is applied with a warning,
+        since only a restart with the list can fix that. `applied_at` always
+        moves, so two applies in one second still reload (deploy() keys on it).
+        """
+        try:
+            deploy = load_deploy(body)
+        except DeployError as exc:
+            return 422, {"error": "the deployment file does not load", "problems": exc.lines}
+        except ConfigError as exc:
+            return 422, {"error": "the deployment file does not load", "problems": [str(exc)]}
+        refused = [f"deploy file: profiles.{name}: provider {p.provider} is not served by the "
+                   f"hosted gateway until phase 2a.6" for name, p in sorted(deploy.profiles.items())
+                   if p.provider in NOT_SERVED]
+        if refused:
+            return 422, {"error": "the deployment file names a provider 2a does not serve",
+                         "problems": refused}
+        warnings = [f"profile {name}'s credential {p.credential} was not handed to the control "
+                    f"plane: add it to hosted/claudebox.env and run `claudebox.sh hosted deploy "
+                    f"--target local`, which lists it in CLAUDEBOX_CREDENTIALS and restarts"
+                    for name, p in sorted(deploy.profiles.items())
+                    if p.credential not in self._candidates]
+        now = int(self.clock())
+        digest = hashlib.sha256(body).hexdigest()
+        with self.records.tx() as c:
+            prev = c.execute("SELECT applied_at FROM deploy_config WHERE id = 1").fetchone()
+            at = max(now, prev[0] + 1) if prev else now
+            c.execute("INSERT OR REPLACE INTO deploy_config (id, body, applied_at, applied_by) "
+                      "VALUES (1, ?, ?, ?)", (body, at, int(actor_id)))
+            c.execute("INSERT INTO audit (at, actor_id, action, scope, detail) VALUES (?, ?, ?, ?, ?)",
+                      (now, int(actor_id), "config_apply", "deployment",
+                       json.dumps({"login": actor_login, "sha256": digest,
+                                   "profiles": sorted(deploy.profiles)})))
+        self.log(f"config apply by {actor_login} (id {actor_id}): {len(deploy.profiles)} "
+                 f"profile(s), sha256 {digest[:12]}")
+        self.deploy()
+        return 200, {"applied_at": at, "sha256": digest, "profiles": sorted(deploy.profiles),
+                     "warnings": warnings, "poll_seconds": deploy.poll_seconds}
+
+    def status_snapshot(self) -> Dict[str, Any]:
+        """`hosted status`: the facts a person needs, and no secret, body or transcript.
+
+        Withheld findings are named by repo, PR, persona and detector only: a
+        withheld body, even redacted, is the transcript_readers' (2b's
+        `withheld`), not an operator's.
+        """
+        now = int(self.clock())
+        conn = self.records.conn()
+        row = conn.execute("SELECT applied_at, applied_by FROM deploy_config WHERE id = 1").fetchone()
+        deploy = self._deploy
+        jobs = [{"job_id": j.job_id, "repo": j.repo, "pr": j.pr, "head": j.head[:12], "mode": j.mode,
+                 "profile": j.profile, "personas": [p.persona for p in j.pairs],
+                 "completed": dict(j.completed)} for j in self.jobs.all()]
+        parks = [{"repo": repo, "pr": pair.pr, "mode": pair.mode, "persona": pair.persona,
+                  "reason": park.reason, "until": park.until}
+                 for (repo, pair), park in sorted(list(self.committer.parks.items()),
+                                                  key=lambda kv: (kv[0][0], kv[0][1]))]
+        outbox = [{"installation_id": r[0], "items": r[1], "oldest_age": now - r[2]}
+                  for r in conn.execute("SELECT installation_id, COUNT(*), MIN(created_at) "
+                                        "FROM outbox GROUP BY installation_id ORDER BY 1")]
+        withheld = []
+        for repo, count in conn.execute("SELECT repo, COUNT(*) FROM withheld GROUP BY repo ORDER BY 1"):
+            newest = conn.execute("SELECT pr, persona, detector, created_at FROM withheld "
+                                  "WHERE repo = ? ORDER BY created_at DESC, id DESC LIMIT 1",
+                                  (repo,)).fetchone()
+            withheld.append({"repo": repo, "count": count, "pr": newest[0], "persona": newest[1],
+                             "detector": newest[2], "at": newest[3]})
+        return {
+            "state": self.state, "reason": self.reason, "now": now,
+            "deployment": ({"applied_at": row[0], "applied_by": row[1],
+                            "profiles": sorted(deploy.profiles) if deploy else []} if row else None),
+            "repos": [{"repo": repo, "installation_id": inst}
+                      for repo, inst in sorted(list(self.scheduler.installation_of.items()))],
+            "jobs": jobs,
+            "parked_pairs": parks,
+            "parked_profiles": {name: p.parked_until
+                                for name, p in sorted(list(self.committer.profiles.items()))
+                                if p.parked_until > now},
+            "deferred_in_a_row": {repo: n for repo, n in sorted(list(self.committer.defers_in_a_row.items()))
+                                  if n},
+            "outbox": outbox,
+            "withheld": withheld,
+            "pages": self.pages.recent(),
+        }
+
     # -- the lock and startup ---------------------------------------------------
     def take_lock(self) -> bool:
         if not self.lock.try_acquire():
@@ -407,6 +537,31 @@ def keys_from_env(env: Mapping[str, str], crypto_mod, app_cls,
     return sealer, app
 
 
+def admin_from_env(env: Mapping[str, str], status, app,
+                   log: Callable[[str], None] = lambda line: print(line, flush=True)):
+    """The admin API's checker and roles, from the platform's values.
+
+    The roles are read once: only `hosted deploy --admins` changes them, and
+    it restarts the container. A list that does not parse is named in the log
+    and leaves every role-gated call refused.
+    """
+    client_id, secret = env.get("GITHUB_APP_CLIENT_ID"), env.get("GITHUB_APP_CLIENT_SECRET")
+    checker = admin_mod.TokenChecker(client_id, secret) if client_id and secret else None
+    raw = env.get(admin_mod.ADMINS_VAR, "")
+    roles, problem = None, ""
+    if not raw:
+        problem = (f"{admin_mod.ADMINS_VAR} is not set; write ADMINS and run "
+                   f"`claudebox.sh hosted deploy --target local --admins ADMINS`")
+    else:
+        try:
+            roles = admin_mod.parse_admins(raw.encode("utf-8"))
+        except admin_mod.AdminsError as exc:
+            problem = f"{admin_mod.ADMINS_VAR}: {exc}"
+    if problem:
+        log(f"WARN: admin roles: {problem}")
+    return admin_mod.Admin(status, app, checker, roles, problem, log=log)
+
+
 def _listen(value: str, default_port: int):
     host, _, port = value.rpartition(":")
     return host or "0.0.0.0", int(port or default_port)
@@ -434,6 +589,10 @@ def main(env: Mapping[str, str] = os.environ) -> None:
     # and the records store is not touched.
     sealer, app = (keys_from_env(env, crypto, App) if app_id and key and deployment_key
                    else (None, None))
+    admin = admin_from_env(env, status, app)
+    admin_server = admin_mod.make_server(admin, *_listen(env.get("ADMIN_LISTEN", "0.0.0.0:8082"),
+                                                         8082))
+    threading.Thread(target=admin_server.serve_forever, daemon=True).start()
     if app is None:
         status.state, status._reason = NOT_CONFIGURED, NO_KEYS
         print(f"not configured: {NO_KEYS}", flush=True)
diff --git a/hosted/supervisor.py b/hosted/supervisor.py
index 94cc333..0711751 100644
--- a/hosted/supervisor.py
+++ b/hosted/supervisor.py
@@ -30,12 +30,16 @@ import time
 from dataclasses import dataclass
 from typing import Callable, Dict, List, Mapping, Optional, Sequence
 
+# The client id and secret are the App's OAuth client: the admin API asks
+# GitHub with them whether this App issued a user token (2a.5).
 CONTROL_SECRETS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET",
-                   "CLAUDEBOX_DEPLOYMENT_KEY")
+                   "GITHUB_APP_CLIENT_ID", "GITHUB_APP_CLIENT_SECRET", "CLAUDEBOX_DEPLOYMENT_KEY")
 # The supervisor's own settings, and the control plane's that it forwards.
 # CLAUDEBOX_DATA is listed so it is never filed as a credential; the volume's
-# path is fixed by the image, so its value is not forwarded.
-CONTROL_SETTINGS = ("PERSONA_DIR", "CHANNEL_LISTEN", "HEALTH_LISTEN")
+# path is fixed by the image, so its value is not forwarded. CLAUDEBOX_ADMINS
+# is the role lists, which `hosted deploy --admins` writes (2a.5).
+CONTROL_SETTINGS = ("PERSONA_DIR", "CHANNEL_LISTEN", "HEALTH_LISTEN", "ADMIN_LISTEN",
+                    "CLAUDEBOX_ADMINS")
 CREDENTIALS_VAR = "CLAUDEBOX_CREDENTIALS"
 SETTINGS = (CREDENTIALS_VAR, "CLAUDEBOX_CHANNEL_URL", "CLAUDEBOX_GATEWAY_URL", "CLAUDEBOX_DRAIN_SECONDS",
             "WORKER_IMAGE", "WORKER_NETWORK", "DOCKER_SOCKET", "CLAUDEBOX_DATA") + CONTROL_SETTINGS
@@ -116,10 +120,10 @@ def credential_names(env: Mapping[str, str]) -> List[str]:
     return names
 
 
-# The spec gives the gateway "the provider and Voyage credentials". The
-# deployment file names only its profiles' credentials, so the list 2a.5
-# writes from it never holds Voyage's: it has one fixed name, and crosses
-# whenever the platform holds it (PR #22 review).
+# The spec gives the gateway "the provider and Voyage credentials". The list
+# `hosted deploy` writes holds every other variable of the env file, and
+# Voyage's is left out of it: it has one fixed name, and crosses whenever the
+# platform holds it (PR #22 review).
 VOYAGE = "VOYAGE_API_KEY"
 
 
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_control.py'`, then `./test-python.sh -p 'test_hosted_supervisor.py'`, then `./test-python.sh`
Expected: PASS, 39 and 22 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/control.py hosted/supervisor.py tests/test_hosted_control.py tests/test_hosted_supervisor.py
git commit -m "hosted: the control plane serves status and config apply"
```

### Task 4: The in-image CLI

**Files:**
- Create: `hosted/hosted_cli.py`
- Test: `tests/test_hosted_cli.py`

**Interfaces:**
- Consumes: Task 1's GitHub calls, Task 2's `parse_admins` and `PREFIX`, 2a.4b's `supervisor` name lists and `control.pem_from_env` (in the tests), 2a.1's `crypto.new_key` (in the image only).
- Produces:
  - The command line `hosted_cli.py admins FILE | credentials NAME... | whois LOGIN | app-create --org ORG --listen HOST:PORT [--name N] [--url U] [--keep-deployment-key] | login [--admin URL] | status [--admin URL] [--json] | config-apply FILE [--admin URL]`, which `main(argv, env, out, err) -> int` parses. Exit 0, or 1 with `ERROR: ...` on stderr.
  - `ADMIN_URL = "http://claudebox:8082"`, `TOKEN_VAR = "CLAUDEBOX_USER_TOKEN"`, `PERMISSIONS`, `EVENTS`, `CliError`.
  - `admins_line(data) -> str`, `credential_list(names) -> list`, `manifest(org, name, url, redirect_url) -> dict`, `ManifestFlow(org, name, url, convert=convert_manifest, state=None)` with `start_page(host)`, `callback(host, query)`, `done`, `result`, `error`; `serve_manifest_flow(flow, host, port, seconds, clock=time.monotonic)`; `env_lines(result, deployment_key) -> list`; `checklist(org, result) -> list`; `admin_call(base, method, path, token="", body=None, transport=urllib_transport) -> (status, payload)`; `login(base, err, admin=admin_call, device_code=..., device_token=..., sleep=time.sleep) -> str`; `render_status(snapshot, pages=10) -> list`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_cli.py`:

```python
import _path  # noqa: F401

import html
import http.client
import io
import json
import re
import threading
import unittest
import urllib.parse

import github_app
import hosted_cli
import supervisor
from control import pem_from_env
from hosted_cli import (CliError, ManifestFlow, admins_line, checklist, credential_list, env_lines,
                        login, main, manifest, render_status, serve_manifest_flow)

RESULT = {"id": 314, "slug": "claudebox-acme", "client_id": "Iv23abc", "client_secret": "cs",
          "webhook_secret": "ws", "pem": "-----BEGIN RSA PRIVATE KEY-----\nAAA\nBBB\n-----END RSA PRIVATE KEY-----\n"}


class AdminsLineTest(unittest.TestCase):
    def test_the_file_becomes_one_compact_line_with_every_role(self):
        self.assertEqual(admins_line(b'{"operators": [43, 42]}'),
                         '{"operators":[42,43],"transcript_readers":[],"erasers":[]}')

    def test_a_refused_file_is_a_cli_error(self):
        with self.assertRaises(CliError) as cm:
            admins_line(b'{"operators": ["octo"]}')
        self.assertIn("hosted whois", str(cm.exception))


class CredentialListTest(unittest.TestCase):
    def test_every_name_claudebox_does_not_own_is_a_credential(self):
        names = ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_CLIENT_SECRET",
                 "CLAUDEBOX_DEPLOYMENT_KEY", "CLAUDEBOX_ADMINS", "CLAUDEBOX_CREDENTIALS",
                 "OLLAMA_API_KEY", "VOYAGE_API_KEY", "ANTHROPIC_KEY_TEAM", "OLLAMA_API_KEY",
                 "GATEWAY_SECRET", "PATH", "", "not-a-name"]
        self.assertEqual(credential_list(names), ["OLLAMA_API_KEY", "ANTHROPIC_KEY_TEAM"])

    def test_the_list_it_writes_is_one_the_supervisor_accepts(self):
        listed = credential_list(list(supervisor.CONTROL_SECRETS) + list(supervisor.SETTINGS) + ["K"])
        self.assertEqual(supervisor.credential_names({"CLAUDEBOX_CREDENTIALS": ",".join(listed)}), ["K"])


class ManifestTest(unittest.TestCase):
    def test_the_manifest_asks_for_the_specs_permissions_with_the_webhook_off(self):
        doc = manifest("acme", "claudebox-acme", "https://x", "http://127.0.0.1:5555/callback")
        self.assertEqual(doc["default_permissions"],
                         {"metadata": "read", "contents": "read", "pull_requests": "read",
                          "issues": "write", "checks": "write", "members": "read"})
        self.assertEqual(doc["hook_attributes"], {"url": "https://x", "active": False})
        self.assertEqual(doc["redirect_url"], "http://127.0.0.1:5555/callback")
        self.assertFalse(doc["public"])
        self.assertIn("push", doc["default_events"])


class ManifestFlowTest(unittest.TestCase):
    def setUp(self):
        self.codes = []

        def convert(code):
            self.codes.append(code)
            return dict(RESULT)

        self.flow = ManifestFlow("acme", "claudebox-acme", "https://x", convert=convert, state="S" * 43)

    def test_the_start_page_posts_the_manifest_with_the_state_to_the_orgs_page(self):
        status, page = self.flow.start_page("127.0.0.1:5555")
        self.assertEqual(status, 200)
        action = re.search(r'action="([^"]+)"', page).group(1)
        self.assertEqual(html.unescape(action),
                         "https://github.com/organizations/acme/settings/apps/new?state=" + "S" * 43)
        value = re.search(r'name="manifest" value="([^"]+)"', page).group(1)
        self.assertEqual(json.loads(html.unescape(value))["redirect_url"],
                         "http://127.0.0.1:5555/callback")

    def test_only_a_loopback_host_and_then_only_that_one(self):
        self.assertEqual(self.flow.start_page("evil.example:5555")[0], 400)
        self.assertEqual(self.flow.start_page("localhost:5555")[0], 200)
        self.assertEqual(self.flow.start_page("127.0.0.1:5555")[0], 400)
        self.assertEqual(self.flow.callback("127.0.0.1:5555", "state=" + "S" * 43 + "&code=c")[0], 400)
        self.assertEqual(self.codes, [])

    def test_a_callback_without_this_flows_state_is_refused_and_the_flow_waits(self):
        self.flow.start_page("127.0.0.1:5555")
        for query in ("code=c", "state=wrong&code=c", "state=" + "S" * 43):
            with self.subTest(query=query):
                self.assertEqual(self.flow.callback("127.0.0.1:5555", query)[0], 400)
        self.assertFalse(self.flow.done)
        self.assertEqual(self.codes, [])

    def test_the_matching_callback_exchanges_the_code_once(self):
        self.flow.start_page("127.0.0.1:5555")
        q = "state=" + "S" * 43 + "&code=the-code"
        self.assertEqual(self.flow.callback("127.0.0.1:5555", q)[0], 200)
        self.assertEqual(self.flow.callback("127.0.0.1:5555", q)[0], 400)
        self.assertEqual(self.codes, ["the-code"])
        self.assertEqual(self.flow.result["id"], 314)

    def test_a_refused_code_ends_the_flow_since_it_is_single_use(self):
        def refuse(code):
            raise github_app.GitHubError(404, "Not Found")
        flow = ManifestFlow("acme", "n", "u", convert=refuse, state="S")
        flow.start_page("127.0.0.1:1")
        self.assertEqual(flow.callback("127.0.0.1:1", "state=S&code=c")[0], 502)
        self.assertTrue(flow.done)
        self.assertIn("refused the manifest code", flow.error)


class ServeTest(unittest.TestCase):
    def test_one_browser_round_trip_over_a_real_socket(self):
        flow = ManifestFlow("acme", "n", "u", convert=lambda code: dict(RESULT, code=code), state="S")
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        t = threading.Thread(target=serve_manifest_flow, args=(flow, "127.0.0.1", port, 10))
        t.start()
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/")
                break
            except ConnectionRefusedError:
                threading.Event().wait(0.05)
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/callback?state=S&code=abc")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(flow.result["code"], "abc")

    def test_no_callback_in_time_is_an_error(self):
        flow = ManifestFlow("acme", "n", "u", convert=lambda c: {}, state="S")
        clock = iter([0, 0, 100, 100])
        serve_manifest_flow(flow, "127.0.0.1", 0, 10, clock=lambda: next(clock))
        self.assertIn("no callback from GitHub", flow.error)


class EnvLinesTest(unittest.TestCase):
    def test_the_secrets_are_one_line_each_and_the_pem_reads_back(self):
        lines = env_lines(RESULT, "dk")
        self.assertEqual([l.split("=", 1)[0] for l in lines],
                         ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_CLIENT_ID",
                          "GITHUB_APP_CLIENT_SECRET", "GITHUB_WEBHOOK_SECRET", "CLAUDEBOX_DEPLOYMENT_KEY"])
        pem = dict(l.split("=", 1) for l in lines)["GITHUB_APP_PRIVATE_KEY"]
        self.assertNotIn("\n", pem)
        self.assertEqual(pem_from_env(pem), RESULT["pem"].strip().encode())

    def test_an_existing_deployment_key_is_never_replaced(self):
        self.assertFalse(any(l.startswith("CLAUDEBOX_DEPLOYMENT_KEY=") for l in env_lines(RESULT, None)))

    def test_an_inactive_webhook_may_have_no_secret(self):
        lines = env_lines(dict(RESULT, webhook_secret=None), None)
        self.assertFalse(any(l.startswith("GITHUB_WEBHOOK_SECRET") for l in lines))

    def test_an_answer_missing_a_secret_is_refused_with_the_cleanup(self):
        with self.assertRaises(CliError) as cm:
            env_lines(dict(RESULT, client_secret=""), None)
        self.assertIn("GITHUB_APP_CLIENT_SECRET", str(cm.exception))
        self.assertIn("delete it", str(cm.exception))

    def test_a_value_that_would_span_lines_is_refused(self):
        with self.assertRaises(CliError):
            env_lines(dict(RESULT, client_secret="a\nb"), None)

    def test_the_checklist_names_the_device_flow_box_and_the_install_page(self):
        text = "\n".join(checklist("acme", RESULT))
        self.assertIn("https://github.com/organizations/acme/settings/apps/claudebox-acme", text)
        self.assertIn("https://github.com/apps/claudebox-acme/installations/new", text)
        self.assertIn("hosted deploy --target local --admins ADMINS", text)


class LoginTest(unittest.TestCase):
    INFO = {"client_id": "Iv23abc", "slug": "claudebox-acme", "owner": "acme",
            "settings_url": "https://github.com/organizations/acme/settings/apps/claudebox-acme"}

    def setUp(self):
        self.err, self.sleeps, self.calls = io.StringIO(), [], []
        self.who = (200, {"id": 42, "login": "octo", "roles": ["operators"], "roles_problem": ""})

    def admin(self, base, method, path, token=""):
        self.calls.append((method, path, token))
        return (200, self.INFO) if path == "/app" else self.who

    def run_login(self, polls, code=None):
        answers = list(polls)
        code = code or {"device_code": "dc", "user_code": "ABCD-1234",
                        "verification_uri": "https://github.com/login/device", "interval": 5,
                        "expires_in": 900}

        def device_code(client_id):
            if isinstance(code, Exception):
                raise code
            return code

        def device_token(client_id, dc):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return login("http://claudebox:8082", self.err, admin=self.admin, device_code=device_code,
                     device_token=device_token, sleep=self.sleeps.append)

    def test_pending_then_granted_and_the_numeric_id_is_printed(self):
        token = self.run_login([{"error": "authorization_pending"}, {"access_token": "ghu_tok"}])
        self.assertEqual(token, "ghu_tok")
        self.assertEqual(self.sleeps, [5, 5])
        self.assertIn(("GET", "/whoami", "ghu_tok"), self.calls)
        text = self.err.getvalue()
        self.assertIn("enter ABCD-1234", text)
        self.assertIn("octo (GitHub user id 42)", text)
        self.assertIn("Roles: operators", text)

    def test_slow_down_lengthens_the_interval(self):
        self.run_login([{"error": "slow_down", "interval": 10}, {"access_token": "t"}])
        self.assertEqual(self.sleeps, [5, 10])

    def test_no_role_names_the_id_and_the_fix(self):
        self.who = (200, {"id": 7, "login": "mallory", "roles": [], "roles_problem": ""})
        self.run_login([{"access_token": "t"}])
        self.assertIn("No role", self.err.getvalue())
        self.assertIn("--admins", self.err.getvalue())

    def test_an_unticked_device_flow_box_names_the_settings_page(self):
        with self.assertRaises(CliError) as cm:
            self.run_login([], code=github_app.DeviceFlowDisabled(""))
        self.assertIn("Enable Device Flow at " + self.INFO["settings_url"], str(cm.exception))

    def test_an_expired_or_denied_code_ends_the_login(self):
        for error, words in (("expired_token", "expired"), ("access_denied", "cancelled")):
            with self.subTest(error=error), self.assertRaises(CliError) as cm:
                self.run_login([{"error": error}])
            self.assertIn(words, str(cm.exception))


class RenderStatusTest(unittest.TestCase):
    def test_every_section_is_rendered(self):
        lines = render_status({
            "state": "serving", "reason": "", "now": 0,
            "deployment": {"applied_at": 0, "applied_by": 42, "profiles": ["ollama"]},
            "repos": [{"repo": "acme/web", "installation_id": 7}],
            "jobs": [{"job_id": "j", "repo": "acme/web", "pr": 12, "head": "abc", "mode": "code",
                      "profile": "ollama", "personas": ["sage"], "completed": {"sage": "ok"}}],
            "parked_pairs": [{"repo": "acme/web", "pr": 12, "mode": "code", "persona": "sme",
                              "reason": "capped", "until": None}],
            "parked_profiles": {"ollama": 60}, "deferred_in_a_row": {"acme/web": 2},
            "outbox": [{"installation_id": 7, "items": 3, "oldest_age": 90}],
            "withheld": [{"repo": "acme/web", "count": 1, "pr": 12, "persona": "sme",
                          "detector": "aws-key", "at": 0}],
            "pages": [{"at": 0, "page": "acme/web deferred 3 times"}]})
        text = "\n".join(lines)
        for words in ("control plane: serving", "profiles: ollama", "acme/web (installation 7)",
                      "acme/web#12 code at abc on ollama: sage (done: sage ok)",
                      "acme/web#12 code/sme: capped", "parked profile: ollama",
                      "deferred in a row: acme/web 2", "3 item(s), oldest 90s",
                      "newest on #12 from sme (aws-key)", "acme/web deferred 3 times"):
            self.assertIn(words, text)

    def test_nothing_applied_says_how(self):
        text = "\n".join(render_status({"state": "not configured", "reason": "no config",
                                        "deployment": None}))
        self.assertIn("hosted config apply FILE", text)


class MainTest(unittest.TestCase):
    def run_main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        rc = main(argv, env=env or {}, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_a_role_gated_call_with_no_cached_login_says_how(self):
        rc, out, err = self.run_main(["status"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("hosted login", err)

    def test_config_apply_prints_every_problem(self):
        def admin(base, method, path, token="", body=None):
            return 422, {"error": "the deployment file does not load",
                         "problems": ["deploy file: nope: unknown key", "deploy file: profiles: x"]}
        old = hosted_cli.admin_call
        hosted_cli.admin_call = admin
        self.addCleanup(setattr, hosted_cli, "admin_call", old)
        import tempfile, os
        fd, path = tempfile.mkstemp()
        os.write(fd, b"{}")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        rc, out, err = self.run_main(["config-apply", path], env={"CLAUDEBOX_USER_TOKEN": "t"})
        self.assertEqual(rc, 1)
        self.assertIn("nope: unknown key", err)
        self.assertIn("profiles: x", err)

    def test_credentials_prints_the_list_on_stdout_only(self):
        rc, out, err = self.run_main(["credentials", "OLLAMA_API_KEY", "GITHUB_APP_ID", "K2"])
        self.assertEqual((rc, out), (0, "OLLAMA_API_KEY,K2\n"))

    def test_an_unreachable_admin_api_names_hosted_deploy(self):
        def down(method, url, headers, body):
            raise github_app.GitHubError(0, "cannot reach GitHub: refused")
        with self.assertRaises(CliError) as cm:
            hosted_cli.admin_call("http://claudebox:8082", "GET", "/app", transport=down)
        self.assertIn("hosted deploy --target local", str(cm.exception))


class ScriptTest(unittest.TestCase):
    def test_it_runs_as_a_script_with_nothing_on_its_path(self):
        # `claudebox.sh` runs it with python3 and no PYTHONPATH, which only the
        # supervisor sets; -I drops the environment and the cwd from the path.
        import os
        import subprocess
        import sys
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hosted", "hosted_cli.py")
        done = subprocess.run([sys.executable, "-I", path, "credentials", "GITHUB_APP_ID", "K"],
                              capture_output=True, text=True, cwd="/", timeout=30)
        self.assertEqual((done.returncode, done.stdout), (0, "K\n"), done.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_cli.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'hosted_cli'`.

- [ ] **Step 3: Implement**

`hosted/hosted_cli.py`:

```python
"""The in-image half of `app create`, `hosted whois`, `hosted login`, `status` and `config apply`.

`claudebox.sh` runs this in a throwaway container of the control-plane image,
the way `config validate` runs `config_cli.py`, so nothing here ever runs on
the host's Python. What the launcher has to keep (an App's secrets, a user
token) is printed on stdout and nothing else is: every line for a person goes
to stderr, so `claudebox.sh` can send stdout straight into the env file or
the token cache without parsing it.

    hosted_cli.py admins FILE          the ADMINS file, checked, as one JSON line
    hosted_cli.py credentials NAME...  which env-file names are provider credentials
    hosted_cli.py whois LOGIN          a login's numeric id (GitHub's public user API)
    hosted_cli.py app-create --org ORG --listen HOST:PORT [--name N] [--url U] [--keep-deployment-key]
    hosted_cli.py login [--admin URL]
    hosted_cli.py status [--admin URL] [--json]
    hosted_cli.py config-apply FILE [--admin URL]

The role-gated three read the user token from CLAUDEBOX_USER_TOKEN, which the
launcher passes by name, so it is never in an argv.
"""

import argparse
import html
import hmac
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

# Run as a script by `claudebox.sh`, outside the supervisor that sets
# PYTHONPATH for the deployment's processes (overview decision 19).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..", "reviewer")]

import admin as admin_mod  # noqa: E402
import github_app  # noqa: E402
import supervisor  # noqa: E402
from config_load import ConfigError  # noqa: E402

ADMIN_URL = "http://claudebox:8082"
TOKEN_VAR = "CLAUDEBOX_USER_TOKEN"
APP_CREATE_SECONDS = 15 * 60
DEFAULT_URL = "https://github.com/MrJoy/claudebox"
# The spec's "Creating the App": every permission is the control plane's.
PERMISSIONS = {"metadata": "read", "contents": "read", "pull_requests": "read",
               "issues": "write", "checks": "write", "members": "read"}
EVENTS = ["pull_request", "pull_request_review", "pull_request_review_comment", "issue_comment",
          "installation", "installation_repositories", "repository", "push"]
_HOST_RE = re.compile(r"(127\.0\.0\.1|localhost):([0-9]{1,5})")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,33}")


class CliError(Exception):
    """A refusal to report on stderr, with exit status 1."""


# -- admins and credentials ---------------------------------------------------------
def admins_line(data: bytes) -> str:
    """The ADMINS file as the one line `CLAUDEBOX_ADMINS` holds: compact, ids sorted."""
    try:
        roles = admin_mod.parse_admins(data)
    except admin_mod.AdminsError as exc:
        raise CliError(str(exc)) from None
    return json.dumps({r: sorted(roles[r]) for r in admin_mod.ROLES}, separators=(",", ":"))


def credential_list(names: Sequence[str]) -> List[str]:
    """The env file's names that are provider credentials: everything claudebox does not own.

    On the local target the env file is claudebox's alone (written by `app
    create`, `hosted deploy` and the operator), so a name the supervisor does
    not know as its own is a credential a profile may name. Voyage's key is
    left out because it crosses by its fixed name.
    """
    own = (set(supervisor.CONTROL_SECRETS) | set(supervisor.SETTINGS) | supervisor.PROCESS_VARS
           | supervisor.RESERVED | {supervisor.VOYAGE})
    out = []
    for name in names:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in own and name not in out:
            out.append(name)
    return out


# -- app create: the manifest flow ----------------------------------------------------
def manifest(org: str, name: str, url: str, redirect_url: str) -> Dict[str, Any]:
    """The App's manifest for the local target: the webhook inactive, the poll does the work."""
    return {"name": name, "url": url, "description": "claudebox: adversarial PR review (hosted)",
            "public": False, "redirect_url": redirect_url,
            "hook_attributes": {"url": url, "active": False},
            "default_permissions": dict(PERMISSIONS), "default_events": list(EVENTS),
            "request_oauth_on_install": False, "setup_on_update": False}


class ManifestFlow:
    """One App creation: the start page, then exactly one matching callback.

    The redirect URL is built from the Host header of the first request for
    the start page, because the port the browser reaches is the one Docker
    published, which this process cannot know. Only a loopback Host is
    accepted, and once fixed it is the only one, so a page on another origin
    cannot rebind the flow to itself. The callback must carry the state this
    flow generated, compared in constant time; anything else is refused and
    the flow keeps waiting.
    """

    def __init__(self, org: str, name: str, url: str,
                 convert: Callable[[str], dict] = github_app.convert_manifest,
                 state: Optional[str] = None):
        self.org, self.name, self.url, self.convert = org, name, url, convert
        self.state = state or secrets.token_urlsafe(32)
        self.host: Optional[str] = None
        self.result: Optional[dict] = None
        self.error: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.result is not None or self.error is not None

    def _host_ok(self, host: str) -> bool:
        if not _HOST_RE.fullmatch(host or ""):
            return False
        if self.host is None:
            self.host = host
        return host == self.host

    def start_page(self, host: str) -> Tuple[int, str]:
        if not self._host_ok(host):
            return 400, "<p>Open this page at the 127.0.0.1 address the terminal printed.</p>"
        doc = manifest(self.org, self.name, self.url, f"http://{self.host}/callback")
        action = (f"https://github.com/organizations/{self.org}/settings/apps/new?"
                  + urllib.parse.urlencode({"state": self.state}))
        return 200, (
            "<!doctype html><meta charset=\"utf-8\"><title>claudebox: create the App</title>"
            f"<form id=\"f\" method=\"post\" action=\"{html.escape(action, quote=True)}\">"
            f"<input type=\"hidden\" name=\"manifest\" value=\"{html.escape(json.dumps(doc), quote=True)}\">"
            f"<p>Creating the claudebox App for {html.escape(self.org)}. "
            "<button type=\"submit\">Continue to GitHub</button></p></form>"
            "<script>document.getElementById(\"f\").submit()</script>")

    def callback(self, host: str, query: str) -> Tuple[int, str]:
        if not self._host_ok(host):
            return 400, "<p>Wrong host.</p>"
        params = urllib.parse.parse_qs(query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        if self.done or not hmac.compare_digest(state.encode(), self.state.encode()) or not code:
            return 400, "<p>This is not the callback this flow is waiting for.</p>"
        try:
            self.result = self.convert(code)
        except (github_app.GitHubError, ValueError) as exc:
            # The code is single-use, so a failed exchange ends the flow.
            self.error = f"GitHub refused the manifest code: {exc}"
            return 502, f"<p>{html.escape(self.error)}</p><p>Return to the terminal.</p>"
        return 200, "<p>The App is created. Return to the terminal.</p>"


def serve_manifest_flow(flow: ManifestFlow, host: str, port: int, seconds: float,
                        clock: Callable[[], float] = time.monotonic) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            path, _, query = self.path.partition("?")
            host_header = self.headers.get("Host", "")
            if path == "/":
                status, body = flow.start_page(host_header)
            elif path == "/callback":
                status, body = flow.callback(host_header, query)
            else:
                status, body = 404, "<p>Not found.</p>"
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    server = http.server.HTTPServer((host, port), Handler)
    server.timeout = 1.0
    until = clock() + seconds
    try:
        while not flow.done and clock() < until:
            server.handle_request()
    finally:
        server.server_close()
    if not flow.done:
        flow.error = f"no callback from GitHub within {int(seconds // 60)} minutes"


def env_lines(result: Dict[str, Any], deployment_key: Optional[str]) -> List[str]:
    """The App's secrets as env-file lines, one line each: a PEM's newlines become `\\n`.

    control.pem_from_env reads that spelling back. A value that would still
    span lines is refused rather than written broken.
    """
    pem = str(result.get("pem") or "").strip().replace("\r\n", "\n").replace("\n", "\\n")
    pairs = [("GITHUB_APP_ID", str(result.get("id") or "")),
             ("GITHUB_APP_PRIVATE_KEY", pem),
             ("GITHUB_APP_CLIENT_ID", str(result.get("client_id") or "")),
             ("GITHUB_APP_CLIENT_SECRET", str(result.get("client_secret") or ""))]
    if result.get("webhook_secret"):
        pairs.append(("GITHUB_WEBHOOK_SECRET", str(result["webhook_secret"])))
    if deployment_key is not None:
        pairs.append(("CLAUDEBOX_DEPLOYMENT_KEY", deployment_key))
    missing = [k for k, v in pairs if not v]
    if missing:
        raise CliError(f"GitHub's answer has no {', '.join(missing)}; the App may exist without "
                       f"them, so delete it in the org's settings and run app create again")
    for k, v in pairs:
        if "\n" in v or "\r" in v:
            raise CliError(f"{k} would span more than one env-file line")
    return [f"{k}={v}" for k, v in pairs]


def checklist(org: str, result: Dict[str, Any]) -> List[str]:
    slug = result.get("slug", "")
    return [
        f"The App {slug} (id {result.get('id')}) exists, and its secrets are in hosted/claudebox.env.",
        "What is left:",
        "  1. Add each profile's provider credential to hosted/claudebox.env, one NAME=value line each.",
        "  2. Write ADMINS, a JSON file like {\"operators\": [12345]}: numeric GitHub user ids only;",
        "     `claudebox.sh hosted whois LOGIN` prints a login's id.",
        "  3. Run `claudebox.sh hosted deploy --target local --admins ADMINS`.",
        f"  4. Tick Enable Device Flow at https://github.com/organizations/{org}/settings/apps/{slug}",
        "     (an App manifest cannot set it).",
        "  5. Run `claudebox.sh hosted login`, then `claudebox.sh hosted config apply claudebox.deploy.json`.",
        f"  6. Install the App on the repos it reviews: https://github.com/apps/{slug}/installations/new",
        "     (and on the org's .github repo too, if it holds org defaults).",
    ]


# -- the admin API's client ----------------------------------------------------------
def admin_call(base: str, method: str, path: str, token: str = "", body: Optional[bytes] = None,
               transport=github_app.urllib_transport) -> Tuple[int, Dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": github_app.USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = transport(method, base.rstrip("/") + admin_mod.PREFIX + path, headers, body)
    except github_app.GitHubError as exc:
        raise CliError(f"cannot reach the control plane's admin API at {base}: {exc.message}; "
                       f"`claudebox.sh hosted deploy --target local` starts it and reports its "
                       f"state") from None
    try:
        payload = json.loads(resp.body) if resp.body else {}
    except ValueError:
        payload = {"error": resp.body[:200].decode("utf-8", "replace")}
    return resp.status, payload if isinstance(payload, dict) else {"error": str(payload)}


def _refusal(status: int, payload: Dict[str, Any]) -> CliError:
    lines = [f"{payload.get('error') or f'HTTP {status}'}"]
    lines += [f"  {p}" for p in payload.get("problems", [])]
    return CliError("\n".join(lines))


def login(base: str, err: TextIO, admin=admin_call, device_code=github_app.device_code,
          device_token=github_app.device_token, sleep: Callable[[float], None] = time.sleep) -> str:
    """The device flow for the App's user OAuth. Returns the token; tells the person the rest."""
    status, info = admin(base, "GET", "/app")
    if status != 200:
        raise _refusal(status, info)
    settings = info.get("settings_url") or "the App's settings page"
    try:
        code = device_code(info["client_id"])
    except github_app.DeviceFlowDisabled:
        raise CliError(f"GitHub refused the device flow, which the App's settings must enable: "
                       f"tick Enable Device Flow at {settings}, then run hosted login again") from None
    minutes = max(1, int(code.get("expires_in", 900)) // 60)
    print(f"Open {code['verification_uri']} and enter {code['user_code']} "
          f"(the code expires in {minutes} minutes).", file=err, flush=True)
    interval = int(code.get("interval", 5))
    tries = int(code.get("expires_in", 900)) // max(interval, 1) + 2
    for _ in range(tries):
        sleep(interval)
        try:
            answer = device_token(info["client_id"], code["device_code"])
        except github_app.DeviceFlowDisabled:
            raise CliError(f"tick Enable Device Flow at {settings}, then run hosted login again") from None
        if answer.get("access_token"):
            token = answer["access_token"]
            break
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = int(answer.get("interval", interval + 5))
            continue
        if error == "expired_token":
            raise CliError("the code expired before it was entered; run hosted login again")
        if error == "access_denied":
            raise CliError("the login was cancelled on GitHub")
        raise CliError(f"GitHub ended the login: {error or 'no answer'}")
    else:
        raise CliError("the code expired before it was entered; run hosted login again")
    status, who = admin(base, "GET", "/whoami", token)
    if status != 200:
        raise _refusal(status, who)
    roles = who.get("roles") or []
    print(f"Logged in as {who['login']} (GitHub user id {who['id']}).", file=err)
    if roles:
        print(f"Roles: {', '.join(roles)}.", file=err)
    else:
        why = who.get("roles_problem") or (f"id {who['id']} is in no role list")
        print(f"No role: {why}. {admin_mod.ROLES_HINT}.", file=err)
    return token


# -- status ---------------------------------------------------------------------------
def _utc(stamp: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp)) if stamp else "-"


def render_status(s: Dict[str, Any], pages: int = 10) -> List[str]:
    out = [f"control plane: {s['state']}" + (f" ({s['reason']})" if s.get("reason") else "")]
    dep = s.get("deployment")
    out.append(f"deployment config: applied {_utc(dep['applied_at'])} by GitHub user id "
               f"{dep['applied_by']}; profiles: {', '.join(dep['profiles']) or '-'}" if dep
               else "deployment config: none applied (`claudebox.sh hosted config apply FILE`)")
    repos = s.get("repos") or []
    out.append(f"repos reviewed: {len(repos)}")
    out += [f"  {r['repo']} (installation {r['installation_id']})" for r in repos]
    jobs = s.get("jobs") or []
    out.append(f"jobs running: {len(jobs)}")
    for j in jobs:
        done = ", ".join(f"{p} {o}" for p, o in sorted(j.get("completed", {}).items()))
        out.append(f"  {j['repo']}#{j['pr']} {j['mode']} at {j['head']} on {j['profile']}: "
                   f"{', '.join(j['personas'])}" + (f" (done: {done})" if done else ""))
    parks = s.get("parked_pairs") or []
    out.append(f"parked pairs: {len(parks)}")
    out += [f"  {p['repo']}#{p['pr']} {p['mode']}/{p['persona']}: {p['reason']}"
            + (f" until {_utc(p['until'])}" if p.get("until") else "") for p in parks]
    for name, until in sorted((s.get("parked_profiles") or {}).items()):
        out.append(f"parked profile: {name} until {_utc(until)}")
    for repo, n in sorted((s.get("deferred_in_a_row") or {}).items()):
        out.append(f"deferred in a row: {repo} {n}")
    for o in s.get("outbox") or []:
        out.append(f"outbox: installation {o['installation_id']}: {o['items']} item(s), oldest "
                   f"{o['oldest_age']}s")
    for w in s.get("withheld") or []:
        out.append(f"withheld findings: {w['repo']}: {w['count']}; newest on #{w['pr']} from "
                   f"{w['persona']} ({w['detector']}) at {_utc(w['at'])}")
    recent = s.get("pages") or []
    out.append(f"pages: {len(recent)}" + (f", newest {min(pages, len(recent))} shown" if recent else ""))
    out += [f"  {_utc(p['at'])}  {p['page']}" for p in recent[-pages:]]
    return out


# -- main ---------------------------------------------------------------------------
def _token(env) -> str:
    token = env.get(TOKEN_VAR, "")
    if not token:
        raise CliError("no cached login; run `claudebox.sh hosted login`")
    return token


def main(argv: Sequence[str], env=os.environ, out: TextIO = sys.stdout,
         err: TextIO = sys.stderr) -> int:
    p = argparse.ArgumentParser(prog="hosted_cli.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("admins"); a.add_argument("file")
    c = sub.add_parser("credentials"); c.add_argument("names", nargs="*")
    w = sub.add_parser("whois"); w.add_argument("login")
    m = sub.add_parser("app-create")
    m.add_argument("--org", required=True); m.add_argument("--listen", required=True)
    m.add_argument("--name"); m.add_argument("--url", default=DEFAULT_URL)
    m.add_argument("--keep-deployment-key", action="store_true")
    for name in ("login", "status", "config-apply"):
        s = sub.add_parser(name)
        s.add_argument("--admin", default=ADMIN_URL)
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "config-apply":
            s.add_argument("file")
    args = p.parse_args(list(argv))
    try:
        if args.cmd == "admins":
            with open(args.file, "rb") as f:
                print(admins_line(f.read()), file=out)
        elif args.cmd == "credentials":
            names = credential_list(args.names)
            print(",".join(names), file=out)
            print(f"provider credentials: {', '.join(names) or 'none'}", file=err)
        elif args.cmd == "whois":
            try:
                user = github_app.public_user(args.login)
            except ValueError as exc:
                raise CliError(str(exc)) from None
            except github_app.GitHubError as exc:
                raise CliError(f"GitHub has no user {args.login}: {exc}" if exc.status == 404
                               else str(exc)) from None
            print(f"{user['login']}: GitHub user id {user['id']}", file=err)
            print(user["id"], file=out)
        elif args.cmd == "app-create":
            return _app_create(args, out, err)
        elif args.cmd == "login":
            print(login(args.admin, err), file=out)
        elif args.cmd == "status":
            status, payload = admin_call(args.admin, "GET", "/status", _token(env))
            if status != 200:
                raise _refusal(status, payload)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True), file=out)
            else:
                print("\n".join(render_status(payload)), file=out)
        elif args.cmd == "config-apply":
            with open(args.file, "rb") as f:
                body = f.read()
            status, payload = admin_call(args.admin, "PUT", "/config", _token(env), body)
            if status != 200:
                raise _refusal(status, payload)
            print(f"Applied: profiles {', '.join(payload['profiles'])} (sha256 "
                  f"{payload['sha256'][:12]}); the next poll, within {payload['poll_seconds']}s, "
                  f"dispatches under it.", file=err)
            for warning in payload.get("warnings", []):
                print(f"WARN: {warning}", file=err)
    except (CliError, ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=err)
        return 1
    return 0


def _app_create(args, out: TextIO, err: TextIO) -> int:
    if not _LOGIN_RE.fullmatch(args.org):
        raise CliError(f"not a GitHub organization: {args.org!r}")
    name = args.name or f"claudebox-{args.org}"[:34]
    if not _NAME_RE.fullmatch(name):
        raise CliError(f"not a usable App name: {name!r} (34 characters at most)")
    host, _, port = args.listen.rpartition(":")
    flow = ManifestFlow(args.org, name, args.url)
    print(f"Waiting for the browser (up to {APP_CREATE_SECONDS // 60} minutes).", file=err, flush=True)
    serve_manifest_flow(flow, host or "0.0.0.0", int(port), APP_CREATE_SECONDS)
    if flow.error is not None:
        raise CliError(flow.error)
    import crypto   # the control-plane image's; only this one subcommand needs it
    key = None if args.keep_deployment_key else crypto.new_key()
    lines = env_lines(flow.result, key)
    print("\n".join(lines), file=out, flush=True)
    print("\n".join(checklist(args.org, flow.result)), file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_cli.py'`, then `./test-python.sh`
Expected: PASS, 30 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/hosted_cli.py tests/test_hosted_cli.py
git commit -m "hosted: the in-image half of the operator commands"
```

### Task 5: The launcher

**Files:**
- Modify: `claudebox.sh`
- Test: `test-launcher.sh`

**Interfaces:**
- Consumes: Task 4's command line; 2a.4b's `hosted/compose.json` (the `claudebox` service and network, the `build` profile) and `/healthz` on `127.0.0.1:8081`.
- Produces: the commands `hosted deploy --target local [--admins FILE]`, `app create --org ORG --target local [--app-name NAME] [--url URL]`, `hosted whois LOGIN`, `hosted login`, `hosted status [--json]`, `hosted config apply FILE`, `hosted logs` and `hosted stop`. Also `abs_path`, now shared with `config validate`, and every command refusing an option it does not take.

- [ ] **Step 1: Write the failing test**

Apply this change to `test-launcher.sh`:

```diff
diff --git a/test-launcher.sh b/test-launcher.sh
index 9d25570..ecd41fb 100755
--- a/test-launcher.sh
+++ b/test-launcher.sh
@@ -485,5 +485,119 @@ if selected "$L"; then
     "!$WORK/dash-operator/-:/config/org.json:ro"
 fi
 
+# --- 2a.5: config's own options, and the hosted operator surface ---------
+
+# Phase 1's F7: --repo on config validate was ignored, and read as checking a repo.
+L="config validate: --repo is refused, naming --repo-name"
+if selected "$L"; then
+  launch "$L" 0 -- -- config validate "$CFG" --repo "$REPO"
+  expect "$L" 1 -- "does not take --repo" "--repo-name OWNER/NAME" '!docker run'
+fi
+
+L="config validate: anything after -- is refused"
+if selected "$L"; then
+  launch "$L" 0 -- -- config validate "$CFG" -- --network host
+  expect "$L" 1 -- "takes nothing after --" '!docker run'
+fi
+
+L="config validate: a run option is refused"
+if selected "$L"; then
+  launch "$L" 0 -- -- config validate "$CFG" --all
+  expect "$L" 1 -- "config validate does not take --all" '!docker run'
+fi
+
+L="hosted: the hosted options are refused on run"
+if selected "$L"; then
+  launch "$L" 0 -- -- "${RUN[@]}" --target local
+  expect "$L" 1 -- "--target only applies to the hosted and app commands" '!docker run'
+fi
+
+L="hosted deploy: needs --target local, and names the phase of the others"
+if selected "$L"; then
+  launch "$L" 0 -- -- hosted deploy
+  expect "$L (none)" 1 -- "needs --target local"
+  launch "$L" 0 -- -- hosted deploy --target fly
+  expect "$L (fly)" 1 -- "arrives in phase 4"
+  launch "$L" 0 -- -- hosted deploy --target gcp
+  expect "$L (gcp)" 1 -- "arrives in phase 5"
+fi
+
+L="hosted deploy: builds both images, checks ADMINS offline, starts compose"
+if selected "$L"; then
+  printf '{"operators":[42]}\n' >"$WORK/admins.json"
+  launch "$L" 0 -- -- hosted deploy --target local --admins "$WORK/admins.json"
+  expect "$L" 0 -- "compose.json --profile build build" \
+    "--network none -v $WORK/admins.json:/config/admins.json:ro --entrypoint python3 claudebox-control" \
+    "hosted_cli.py admins /config/admins.json" "set CLAUDEBOX_ADMINS" \
+    "hosted_cli.py credentials" "set CLAUDEBOX_CREDENTIALS" "compose.json up -d claudebox" \
+    "--cap-drop ALL" "--user 65534:65534"
+fi
+
+L="hosted deploy: a missing ADMINS file stops before anything is changed"
+if selected "$L"; then
+  launch "$L" 0 -- -- hosted deploy --target local --admins "$WORK/no-such-admins.json"
+  expect "$L" 1 -- "--admins not found" '!set CLAUDEBOX_ADMINS' '!up -d'
+fi
+
+L="app create: needs --org, and publishes its port on loopback only"
+if selected "$L"; then
+  launch "$L" 0 -- -- app create --target local
+  expect "$L (no org)" 1 -- "needs --org ORG"
+  launch "$L" 0 -- -- app create --org acme --target local --app-name "claudebox acme"
+  expect "$L" 0 -- "-p 127.0.0.1::8765" "app-create --org acme --listen 0.0.0.0:8765" \
+    "--name claudebox\\ acme" '!-p 8765' '!0.0.0.0::8765'
+fi
+
+L="hosted status: the cached token crosses by name only"
+if selected "$L"; then
+  mkdir -p "$WORK/.config/claudebox"
+  printf 'ghu_cached_secret_token' >"$WORK/.config/claudebox/hosted-local.token"
+  launch "$L" 0 -- -- hosted status --json
+  expect "$L" 0 -- "--network claudebox -e CLAUDEBOX_USER_TOKEN --entrypoint python3 claudebox-control" \
+    "hosted_cli.py status --json" '!ghu_cached_secret_token'
+fi
+
+L="hosted config apply: the file is mounted read-only"
+if selected "$L"; then
+  launch "$L" 0 -- -- hosted config apply "$DEP"
+  expect "$L" 0 -- "$DEP:/config/claudebox.deploy.json:ro" \
+    "hosted_cli.py config-apply /config/claudebox.deploy.json"
+  launch "$L" 0 -- -- hosted config apply
+  expect "$L (no file)" 1 -- "takes one FILE"
+fi
+
+L="hosted whois and login: the network each needs, and no more"
+if selected "$L"; then
+  launch "$L" 0 -- -- hosted whois octocat
+  expect "$L (whois)" 0 -- "hosted_cli.py whois octocat" '!--network claudebox'
+  launch "$L" 0 -- -- hosted login
+  expect "$L (login)" 0 -- "--network claudebox --entrypoint python3 claudebox-control" "hosted_cli.py login"
+fi
+
+# The control image's entrypoint is the supervisor: a CLI call without
+# --entrypoint hands its arguments to the supervisor, which starts the
+# deployment's three processes instead.
+L="hosted: every in-image CLI call replaces the supervisor entrypoint"
+if selected "$L"; then
+  : >"$WORK/cli-lines"
+  for cmd in "hosted deploy --target local --admins $WORK/admins.json" "hosted whois octocat" \
+             "hosted login" "hosted status" "hosted config apply $DEP" "app create --org acme --target local"; do
+    # shellcheck disable=SC2086
+    launch "$L" 0 -- -- $cmd
+    grep 'hosted_cli.py' "$WORK/out" >>"$WORK/cli-lines"
+  done
+  if [ "$(grep -c . "$WORK/cli-lines")" -ge 7 ] && ! grep -v -- '--entrypoint python3 claudebox-control /opt/claudebox/hosted/hosted_cli.py' "$WORK/cli-lines" >/dev/null; then
+    ok "$L"
+  else
+    cp "$WORK/cli-lines" "$WORK/out"; bad "$L" "a CLI call without --entrypoint python3"
+  fi
+fi
+
+L="hosted: an unknown subcommand lists the real ones"
+if selected "$L"; then
+  launch "$L" 0 -- -- hosted deploi --target local
+  expect "$L" 1 -- "unknown hosted subcommand 'deploi'"
+fi
+
 printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
 [ "$FAIL" = 0 ] || { printf 'Failed:%s\n' "$FAILED"; exit 1; }
```

- [ ] **Step 2: Run it to see it fail**

Run: `/bin/bash ./test-launcher.sh`
Expected: FAIL in each new case, starting with `config validate: --repo is refused, naming --repo-name` (the launcher accepts the option and prints the docker command).

- [ ] **Step 3: Implement**

Apply this change to `claudebox.sh`:

```diff
diff --git a/claudebox.sh b/claudebox.sh
index fd52544..a864cbc 100755
--- a/claudebox.sh
+++ b/claudebox.sh
@@ -36,6 +36,17 @@ CONFIG_ORG=""
 CONFIG_DEPLOY=""
 CONFIG_REPO_NAME=""
 
+# `hosted ...` and `app create` (the hosted App's operator surface). Every
+# option given is remembered, so a command can refuse one it does not take.
+HOSTED_POS=()
+TARGET=""
+ADMINS_FILE=""
+APP_NAME=""
+APP_URL=""
+STATUS_JSON=0
+GIVEN=""
+CONTROL_IMAGE="claudebox-control"
+
 # PR selectors (mutually exclusive; passed through to the container as -e VARs).
 PR_ALL=0
 PR_ASSIGNEE=""
@@ -90,6 +101,32 @@ COMMANDS
             lets that check read the org kindex store's share_with list.
             Local mode (run/test) does not read this file.
 
+HOSTED COMMANDS (the GitHub App, on this machine's Docker; see README)
+  hosted deploy --target local [--admins FILE]
+            Build both images and start (or update) the control plane with
+            docker compose. Safe to re-run: the container is recreated only
+            when the image or hosted/claudebox.env changed. --admins writes
+            the admin roles (a JSON file of numeric GitHub user ids) into
+            hosted/claudebox.env, which restarts the control plane. Reports
+            the control plane's state when it is up.
+  app create --org ORG --target local [--app-name NAME] [--url URL]
+            Create the GitHub App through GitHub's manifest flow, in the
+            browser, and write its secrets into hosted/claudebox.env. Run
+            hosted deploy first. Prints the checklist of what is left.
+  hosted whois LOGIN
+            Print a GitHub login's numeric user id, for the ADMINS file.
+  hosted login
+            Log in to the control plane through GitHub's device flow and
+            cache the token (~/.config/claudebox/hosted-local.token).
+  hosted status [--json]
+            The deployment's state: repos, jobs, parked pairs, the outbox,
+            withheld findings and recent pages. Needs the operators role.
+  hosted config apply FILE
+            Apply a deployment file (claudebox.deploy.json). Needs the
+            operators role. Takes effect on the next poll, without a restart.
+  hosted logs / hosted stop
+            Follow the control plane's log; stop it (its volume is kept).
+
 OPTIONS
   --repo PATH       Host repo to seed the reviewer from (default: current dir).
                     Only its .git is mounted, read-only at /repo/.git: the
@@ -197,18 +234,26 @@ EOF
 COMMAND=""
 EXTRA=()
 while [ $# -gt 0 ]; do
+  case "$1" in --) : ;; -?*) GIVEN="$GIVEN $1" ;; esac
   case "$1" in
-    build|run|test|logs|shell|stop|status|config)
-      # After `config`, every bare word is its own positional: a file may
-      # be called `run` or `test`.
-      if [ "$COMMAND" = config ]; then CONFIG_POS+=("$1")
-      else
-        [ -z "$COMMAND" ] || die "more than one command given ('$COMMAND' and '$1')."
-        COMMAND="$1"
-      fi ;;
-    --org)         CONFIG_ORG="${2:?--org requires a FILE}"; shift ;;
+    build|run|test|logs|shell|stop|status|config|hosted|app)
+      # After `config`, `hosted` or `app`, every bare word is its own
+      # positional: a file may be called `run` or `test`.
+      case "$COMMAND" in
+        config) CONFIG_POS+=("$1") ;;
+        hosted|app) HOSTED_POS+=("$1") ;;
+        *)
+          [ -z "$COMMAND" ] || die "more than one command given ('$COMMAND' and '$1')."
+          COMMAND="$1" ;;
+      esac ;;
+    --org)         CONFIG_ORG="${2:?--org requires a value}"; shift ;;
     --deploy)      CONFIG_DEPLOY="${2:?--deploy requires a FILE}"; shift ;;
     --repo-name)   CONFIG_REPO_NAME="${2:?--repo-name requires OWNER/NAME}"; shift ;;
+    --target)      TARGET="${2:?--target requires local, fly or gcp}"; shift ;;
+    --admins)      ADMINS_FILE="${2:?--admins requires a FILE}"; shift ;;
+    --app-name)    APP_NAME="${2:?--app-name requires a NAME}"; shift ;;
+    --url)         APP_URL="${2:?--url requires a URL}"; shift ;;
+    --json)        STATUS_JSON=1 ;;
     --repo)        REPO="${2:?--repo requires a PATH}"; MOUNT_REPO=1; REPO_EXPLICIT=1; shift ;;
     --no-repo)     MOUNT_REPO=0 ;;
     --env-file)    ENV_FILE="${2:?--env-file requires a PATH}"; ENV_FILE_EXPLICIT=1; shift ;;
@@ -237,18 +282,52 @@ while [ $# -gt 0 ]; do
     --)            shift; EXTRA=("$@"); break ;;
     -*)            die "unknown option: $1 (see --help)." ;;
     *)
-      if [ "$COMMAND" = config ]; then CONFIG_POS+=("$1")
-      else die "unknown argument: $1 (see --help)."
-      fi ;;
+      case "$COMMAND" in
+        config) CONFIG_POS+=("$1") ;;
+        hosted|app) HOSTED_POS+=("$1") ;;
+        *) die "unknown argument: $1 (see --help)." ;;
+      esac ;;
   esac
   shift
 done
 
 [ -n "$COMMAND" ] || { usage; exit 2; }
 
-if [ "$COMMAND" != config ] && { [ -n "$CONFIG_ORG" ] || [ -n "$CONFIG_DEPLOY" ] || [ -n "$CONFIG_REPO_NAME" ]; }; then
-  die "--org/--deploy/--repo-name only apply to config validate."
-fi
+# A command that takes a few options refuses every other one, rather than
+# ignoring it: `config validate --repo x` reads as checking repo x and did
+# nothing of the kind (Phase 1's F7). The hint names the likely intent.
+only_options() {
+  local what="$1" opt ok; shift
+  for opt in $GIVEN; do
+    ok=0
+    for allowed in "$@" --dry-run -h --help; do [ "$opt" = "$allowed" ] && ok=1; done
+    if [ "$ok" = 0 ]; then
+      case "$opt" in
+        --repo) [ "$what" = "config validate" ] && die "config validate does not take --repo; --repo-name OWNER/NAME names the repo whose config it checks." ;;
+      esac
+      die "$what does not take $opt (see --help)."
+    fi
+  done
+  [ "${#EXTRA[@]}" = 0 ] || die "$what takes nothing after --."
+}
+case "$COMMAND" in
+  config) only_options "config validate" --org --deploy --repo-name --image --memory --pids --no-repo ;;
+  app)    only_options "app create" --org --target --app-name --url ;;
+  hosted)
+    case "${HOSTED_POS[0]:-}" in
+      deploy) only_options "hosted deploy" --target --admins ;;
+      status) only_options "hosted status" --json ;;
+      whois|login|config|logs|stop) only_options "hosted ${HOSTED_POS[0]}" ;;
+      *)      : ;;   # hosted_command names the subcommands it has
+    esac ;;
+  *)
+    for opt in --org --deploy --repo-name; do
+      case " $GIVEN " in *" $opt "*) die "--org/--deploy/--repo-name only apply to config validate (and --org to app create)." ;; esac
+    done
+    for opt in --target --admins --app-name --url --json; do
+      case " $GIVEN " in *" $opt "*) die "$opt only applies to the hosted and app commands (see --help)." ;; esac
+    done ;;
+esac
 
 # Selector flags are mutually exclusive (the entrypoint is authoritative and
 # also errors when none/multiple are set via the env file; this is the friendly
@@ -328,7 +407,7 @@ derive_name() {
 # Run the resolution appropriate to the command. build and config need
 # nothing; test is ephemeral/unnamed so it skips name derivation.
 case "$COMMAND" in
-  build|config) : ;;
+  build|config|hosted|app) : ;;
   test)  resolve_env_file; resolve_repo ;;
   *)     resolve_env_file; resolve_repo; derive_name ;;
 esac
@@ -486,6 +565,195 @@ build_run_flags() {
         # when it's NOT the final statement executed).
 }
 
+# A file's absolute path for a bind mount. The ./ prefix keeps a name like
+# `-` or `-/o.json` from reaching cd as an option, and CDPATH is cleared so
+# a relative name resolves against the cwd alone.
+abs_path() {
+  case "$1" in /*) set -- "$1" ;; *) set -- "./$1" ;; esac
+  (CDPATH= cd "$(dirname "$1")" && printf '%s/%s' "$(pwd)" "$(basename "$1")")
+}
+
+# --- The hosted App's operator surface ------------------------------------
+# Every in-image half runs in a throwaway container of the control-plane
+# image, as `config validate` does, never on the host's Python. What one
+# hands back to keep (an App's secrets, a user token, a checked roles line)
+# is its stdout; everything for a person is its stderr.
+HOSTED_DIR="$SCRIPT_DIR/hosted"
+HOSTED_ENV="$HOSTED_DIR/claudebox.env"
+HOSTED_NET="claudebox"            # compose.json's network, which the CLI joins to reach the admin API
+HOSTED_CLI=/opt/claudebox/hosted/hosted_cli.py
+TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/claudebox/hosted-local.token"
+
+
+# The CLI container: unprivileged, no capabilities, its own throwaway uid.
+cli_flags() {
+  CLI=(--rm --cap-drop ALL --security-opt no-new-privileges --pids-limit "$PIDS" --memory "$MEMORY"
+       --user 65534:65534)
+}
+
+compose() { show_and_run docker compose -f "$HOSTED_DIR/compose.json" "$@"; }
+
+# Set KEY=VALUE in the env file, replacing any earlier line for KEY. The file
+# is rewritten through a mktemp sibling (mode 600) and renamed into place.
+env_set() {
+  local key="$1" value="$2" tmp
+  if [ "$DRY_RUN" = 1 ]; then log "+ (set $key in $HOSTED_ENV)"; return 0; fi
+  tmp="$(mktemp "$HOSTED_ENV.XXXXXX")"
+  grep -v "^$key=" "$HOSTED_ENV" >"$tmp" || true
+  printf '%s=%s\n' "$key" "$value" >>"$tmp"
+  mv "$tmp" "$HOSTED_ENV"
+}
+
+need_target_local() {
+  [ -n "$TARGET" ] || die "$1 needs --target local."
+  case "$TARGET" in
+    local) : ;;
+    fly|gcp) die "--target $TARGET arrives in phase $( [ "$TARGET" = fly ] && echo 4 || echo 5 ); --target local is the only target in 2a." ;;
+    *) die "unknown --target '$TARGET' (local, fly or gcp)." ;;
+  esac
+}
+
+# The cached user token, handed to the CLI by name so it is never in an argv.
+with_token() {
+  [ "$DRY_RUN" = 1 ] || [ -s "$TOKEN_FILE" ] || die "no cached login; run '$0 hosted login'."
+  if [ "$DRY_RUN" != 1 ]; then CLAUDEBOX_USER_TOKEN="$(cat "$TOKEN_FILE")"; export CLAUDEBOX_USER_TOKEN; fi
+  show_and_run docker run "${CLI[@]}" --network "$HOSTED_NET" -e CLAUDEBOX_USER_TOKEN "$@"
+}
+
+hosted_deploy() {
+  need_target_local "hosted deploy"
+  [ "${#HOSTED_POS[@]}" = 1 ] || die "hosted deploy takes no arguments; --admins FILE names the roles file."
+  if [ ! -f "$HOSTED_ENV" ]; then
+    if [ "$DRY_RUN" = 1 ]; then log "+ (create $HOSTED_ENV, mode 600)"
+    else (umask 077; : >"$HOSTED_ENV"); announce "created $HOSTED_ENV (mode 600): app create writes the App's secrets into it"
+    fi
+  fi
+  [ "$DRY_RUN" = 1 ] || chmod 600 "$HOSTED_ENV"
+  compose --profile build build
+  if [ -n "$ADMINS_FILE" ]; then
+    [ -f "$ADMINS_FILE" ] || die "--admins not found: $ADMINS_FILE"
+    local line
+    line="$(show_and_run docker run "${CLI[@]}" --network none \
+              -v "$(abs_path "$ADMINS_FILE"):/config/admins.json:ro" \
+              --entrypoint python3 "$CONTROL_IMAGE" "$HOSTED_CLI" admins /config/admins.json)" \
+      || die "the ADMINS file was refused; nothing was changed."
+    env_set CLAUDEBOX_ADMINS "$line"
+    [ "$DRY_RUN" = 1 ] || announce "admin roles written to $HOSTED_ENV"
+  fi
+  # The supervisor's credential list: every name in the env file claudebox
+  # does not own. Only names cross into the container, never values.
+  local names=() list
+  if [ -f "$HOSTED_ENV" ]; then
+    while IFS= read -r n; do names+=("$n"); done <<EOF_NAMES
+$(sed -n -E 's/^([A-Za-z_][A-Za-z0-9_]*)=.+$/\1/p' "$HOSTED_ENV")
+EOF_NAMES
+  fi
+  list="$(show_and_run docker run "${CLI[@]}" --network none --entrypoint python3 "$CONTROL_IMAGE" \
+            "$HOSTED_CLI" credentials ${names[@]+"${names[@]}"})" \
+    || die "could not work out the credential list."
+  env_set CLAUDEBOX_CREDENTIALS "$list"
+  compose up -d claudebox
+  [ "$DRY_RUN" = 1 ] && return 0
+  # The control plane's state, which is how a deployment nobody can log
+  # into yet is diagnosed. /healthz is published on loopback only.
+  local state="" i
+  for i in $(seq 1 30); do
+    state="$(curl -fsS --max-time 2 http://127.0.0.1:8081/healthz 2>/dev/null || true)"
+    case "$state" in ""|"waiting for lock"*) sleep 2 ;; *) break ;; esac
+  done
+  log "control plane: ${state:-not answering on 127.0.0.1:8081 yet; '$0 hosted logs' says why}"
+}
+
+app_create() {
+  need_target_local "app create"
+  [ "${#HOSTED_POS[@]}" = 1 ] || die "app create takes no arguments; --org ORG names the organization."
+  [ -n "$CONFIG_ORG" ] || die "app create needs --org ORG, the organization that will own the App."
+  [ -f "$HOSTED_ENV" ] || [ "$DRY_RUN" = 1 ] \
+    || die "no $HOSTED_ENV yet: run '$0 hosted deploy --target local' first, which creates it."
+  if [ -f "$HOSTED_ENV" ] && grep -q '^GITHUB_APP_ID=' "$HOSTED_ENV"; then
+    die "$HOSTED_ENV already holds an App (GITHUB_APP_ID), and a second app create would orphan it. To replace it, delete the App in the organization's settings, remove its GITHUB_APP_* lines, and run app create again."
+  fi
+  local opts=(--org "$CONFIG_ORG" --listen 0.0.0.0:8765) cname="claudebox-app-create" pid port="" i rc
+  [ -z "$APP_NAME" ] || opts+=(--name "$APP_NAME")
+  [ -z "$APP_URL" ] || opts+=(--url "$APP_URL")
+  # The deployment key encrypts blobs; one that exists is never replaced.
+  if [ -f "$HOSTED_ENV" ] && grep -q '^CLAUDEBOX_DEPLOYMENT_KEY=.' "$HOSTED_ENV"; then
+    opts+=(--keep-deployment-key)
+  fi
+  if [ "$DRY_RUN" = 1 ]; then
+    show_and_run docker run "${CLI[@]}" --name "$cname" -p 127.0.0.1::8765 --entrypoint python3 "$CONTROL_IMAGE" \
+      "$HOSTED_CLI" app-create "${opts[@]}"
+    return 0
+  fi
+  # The secrets go from the container's stdout straight to the end of the
+  # env file, so they are in no other file and no terminal.
+  if [ -s "$HOSTED_ENV" ] && [ -n "$(tail -c1 "$HOSTED_ENV")" ]; then printf '\n' >>"$HOSTED_ENV"; fi
+  trap 'docker rm -f "'"$cname"'" >/dev/null 2>&1 || true' INT TERM
+  show_and_run docker run "${CLI[@]}" --name "$cname" -p 127.0.0.1::8765 --entrypoint python3 "$CONTROL_IMAGE" \
+    "$HOSTED_CLI" app-create "${opts[@]}" >>"$HOSTED_ENV" &
+  pid=$!
+  # Docker chose the host port; the browser is opened on it once it is known.
+  for i in $(seq 1 60); do
+    port="$(docker port "$cname" 8765/tcp 2>/dev/null | sed -n 's/^127\.0\.0\.1:\([0-9]*\)$/\1/p' | head -n1)"
+    [ -n "$port" ] && break
+    kill -0 "$pid" 2>/dev/null || break
+    sleep 0.5
+  done
+  if [ -n "$port" ]; then
+    local url="http://127.0.0.1:$port/"
+    log "Opening $url, which sends the App's manifest to GitHub for the org owner to review."
+    if command -v open >/dev/null 2>&1; then open "$url" || log "Open $url in a browser."
+    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || log "Open $url in a browser."
+    else log "Open $url in a browser."
+    fi
+  fi
+  rc=0; wait "$pid" || rc=$?
+  trap - INT TERM
+  [ "$rc" = 0 ] || die "app create did not finish; $HOSTED_ENV holds no new App secret."
+}
+
+hosted_command() {
+  cli_flags
+  local sub="${HOSTED_POS[0]:-}"
+  if [ "$COMMAND" = app ]; then
+    [ "$sub" = create ] || die "app needs a subcommand: app create --org ORG --target local"
+    app_create; return
+  fi
+  case "$sub" in
+    deploy) hosted_deploy ;;
+    whois)
+      [ "${#HOSTED_POS[@]}" = 2 ] || die "hosted whois takes one LOGIN."
+      show_and_run docker run "${CLI[@]}" --entrypoint python3 "$CONTROL_IMAGE" "$HOSTED_CLI" whois "${HOSTED_POS[1]}" ;;
+    login)
+      [ "${#HOSTED_POS[@]}" = 1 ] || die "hosted login takes no arguments."
+      if [ "$DRY_RUN" = 1 ]; then
+        show_and_run docker run "${CLI[@]}" --network "$HOSTED_NET" --entrypoint python3 "$CONTROL_IMAGE" "$HOSTED_CLI" login
+        return 0
+      fi
+      mkdir -p "$(dirname "$TOKEN_FILE")"
+      ( umask 077
+        show_and_run docker run "${CLI[@]}" --network "$HOSTED_NET" --entrypoint python3 "$CONTROL_IMAGE" \
+          "$HOSTED_CLI" login >"$TOKEN_FILE.new" ) \
+        || { rm -f "$TOKEN_FILE.new"; die "not logged in."; }
+      mv "$TOKEN_FILE.new" "$TOKEN_FILE"
+      log "The token is cached in $TOKEN_FILE." ;;
+    status)
+      [ "${#HOSTED_POS[@]}" = 1 ] || die "hosted status takes no arguments."
+      local json=(); [ "$STATUS_JSON" = 1 ] && json=(--json)
+      with_token --entrypoint python3 "$CONTROL_IMAGE" "$HOSTED_CLI" status ${json[@]+"${json[@]}"} ;;
+    config)
+      [ "${HOSTED_POS[1]:-}" = apply ] || die "hosted config needs a subcommand: hosted config apply FILE"
+      [ "${#HOSTED_POS[@]}" = 3 ] || die "hosted config apply takes one FILE."
+      [ -f "${HOSTED_POS[2]}" ] || die "FILE not found: ${HOSTED_POS[2]}"
+      with_token -v "$(abs_path "${HOSTED_POS[2]}"):/config/claudebox.deploy.json:ro" \
+        --entrypoint python3 "$CONTROL_IMAGE" "$HOSTED_CLI" config-apply /config/claudebox.deploy.json ;;
+    logs)  [ "${#HOSTED_POS[@]}" = 1 ] || die "hosted logs takes no arguments."; compose logs -f claudebox ;;
+    stop)  [ "${#HOSTED_POS[@]}" = 1 ] || die "hosted stop takes no arguments."; compose down ;;
+    "")    die "hosted needs a subcommand: deploy, whois, login, status, config apply, logs or stop." ;;
+    *)     die "unknown hosted subcommand '$sub' (deploy, whois, login, status, config apply, logs, stop)." ;;
+  esac
+}
+
 case "$COMMAND" in
   build)
     # Match the image's kindex to the host's, so the image opens the schema the
@@ -538,10 +806,6 @@ case "$COMMAND" in
     fi
     ;;
   config)
-    abs() {
-      case "$1" in /*) set -- "$1" ;; *) set -- "./$1" ;; esac
-      (CDPATH= cd "$(dirname "$1")" && printf '%s/%s' "$(pwd)" "$(basename "$1")")
-    }
     [ "${#CONFIG_POS[@]}" -gt 0 ] || die "config needs a subcommand: config validate FILE"
     [ "${CONFIG_POS[0]}" = validate ] || die "unknown config subcommand '${CONFIG_POS[0]}' (the only one is validate)."
     [ "${#CONFIG_POS[@]}" -ge 2 ] || die "config validate needs a FILE."
@@ -550,13 +814,13 @@ case "$COMMAND" in
     [ -f "$cfg_file" ] || die "FILE not found: $cfg_file"
     [ -z "$CONFIG_ORG" ] || [ -f "$CONFIG_ORG" ] || die "--org not found: $CONFIG_ORG"
     [ -z "$CONFIG_DEPLOY" ] || [ -f "$CONFIG_DEPLOY" ] || die "--deploy not found: $CONFIG_DEPLOY"
-    mounts=(-v "$(abs "$cfg_file"):/config/claudebox.json:ro")
+    mounts=(-v "$(abs_path "$cfg_file"):/config/claudebox.json:ro")
     cli_args=(validate /config/claudebox.json)
     if [ -n "$CONFIG_ORG" ]; then
-      mounts+=(-v "$(abs "$CONFIG_ORG"):/config/org.json:ro"); cli_args+=(--org /config/org.json)
+      mounts+=(-v "$(abs_path "$CONFIG_ORG"):/config/org.json:ro"); cli_args+=(--org /config/org.json)
     fi
     if [ -n "$CONFIG_DEPLOY" ]; then
-      mounts+=(-v "$(abs "$CONFIG_DEPLOY"):/config/claudebox.deploy.json:ro")
+      mounts+=(-v "$(abs_path "$CONFIG_DEPLOY"):/config/claudebox.deploy.json:ro")
       cli_args+=(--deploy /config/claudebox.deploy.json)
     fi
     [ -z "$CONFIG_REPO_NAME" ] || cli_args+=(--repo-name "$CONFIG_REPO_NAME")
@@ -564,4 +828,5 @@ case "$COMMAND" in
       --pids-limit "$PIDS" --memory "$MEMORY" "${mounts[@]}" \
       --entrypoint python3 "$IMAGE" /opt/claudebox/reviewer/config_cli.py "${cli_args[@]}"
     ;;
+  hosted|app) hosted_command ;;
 esac
```

- [ ] **Step 4: Run it to see it pass, then every suite**

Run: `bash -n claudebox.sh && /bin/bash ./test-launcher.sh`, then `./test-python.sh`
Expected: PASS, 74 cases. The Python suite passes.

- [ ] **Step 5: Commit**

```bash
git add claudebox.sh test-launcher.sh
git commit -m "launcher: the hosted operator commands, and config validate refuses what it does not take"
```

### Task 6: The first end-to-end review

**Files:** none. This task is run by a person and recorded as evidence.

**Interfaces:**
- Consumes: Tasks 1 to 5, and 2a.1 to 2a.4b.
- Produces: the evidence that the trial pipeline reviews a PR, recorded in the factory run.

A throwaway GitHub organization is needed, owned by the person running this, with one private repository holding a small change on a branch, and an Ollama Cloud key (or an Anthropic key, or a `custom` endpoint). Run from the repository root on a host with Docker.

- [ ] **Step 1: Bring the deployment up**

```bash
./claudebox.sh hosted deploy --target local
```

Both images build. The last line reads `control plane: not configured: the App credentials or the deployment key are missing or malformed; ...`. `hosted/claudebox.env` exists at mode 600.

- [ ] **Step 2: Create the App**

```bash
./claudebox.sh app create --org THROWAWAY-ORG --target local
```

A browser opens on GitHub's "Create GitHub App" page for the org, listing exactly the spec's six permissions with no webhook. After Create, the terminal prints the checklist, and `hosted/claudebox.env` holds `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (one line), `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` and `CLAUDEBOX_DEPLOYMENT_KEY`. A second run is refused as "already holds an App".

- [ ] **Step 3: Credentials and roles**

Add `OLLAMA_API_KEY=...` to `hosted/claudebox.env`. Write `admins.json` with the runner's own id from `./claudebox.sh hosted whois LOGIN`. Then run:

```bash
./claudebox.sh hosted deploy --target local --admins admins.json
```

The env file gains `CLAUDEBOX_ADMINS={"operators":[ID],...}` and `CLAUDEBOX_CREDENTIALS=OLLAMA_API_KEY`. The last line reads `control plane: not configured: no deployment config in the records store yet; ...`. A run with `{"operators": ["LOGIN"]}` is refused naming `hosted whois`, and leaves the env file unchanged.

- [ ] **Step 4: Log in before and after the device-flow box**

Run `./claudebox.sh hosted login` before ticking Enable Device Flow. It fails, naming the App's settings page. Tick the box and run it again: it prints a code, and after the code is entered at `https://github.com/login/device` it prints `Logged in as LOGIN (GitHub user id ID).` and `Roles: operators.`. Then a token the `gh` CLI holds for the same user is refused: `CLAUDEBOX_USER_TOKEN=$(gh auth token) docker run --rm --network claudebox -e CLAUDEBOX_USER_TOKEN --entrypoint python3 claudebox-control /opt/claudebox/hosted/hosted_cli.py status` prints `ERROR: GitHub does not know this token as one this App issued ...`.

- [ ] **Step 5: Apply a deployment file and install the App**

Write `claudebox.deploy.json` with one `ollama` profile (`credential: OLLAMA_API_KEY`, `timing.poll_seconds: 30`). Then run:

```bash
./claudebox.sh hosted config apply claudebox.deploy.json
./claudebox.sh hosted status
```

The apply prints `Applied: profiles ollama ...`, and `status` shows `control plane: serving` and the deployment applied by the runner's id. Install the App on the private repository, and add `.github/claudebox.json` (`{"profile": "ollama"}`) on its default branch.

- [ ] **Step 6: The first review**

Open a PR from the branch. Within a poll, `hosted status` lists the repo and a running job, and the PR shows a `claudebox` check run reading queued, then in progress. When the job ends, the PR carries signed findings (or none), the check run concludes, and `hosted logs` shows a commit of `ok` for each persona. This is the check run written to GitHub (2a.4a) and the channel serving a real worker (2a.4a).

- [ ] **Step 7: A resumed review**

Push a second commit to the PR. The next job's worker log shows each persona resuming its session (`--resume`) from the transcript the first job's container wrote (2a.3), and the new findings name only the new commit's changes.

- [ ] **Step 8: Stop it**

`./claudebox.sh hosted stop`. The container stops within the drain, and `docker volume ls` still lists `claudebox_claudebox-data`. Uninstall the App and delete the throwaway org's App when the evidence is recorded.

### Task 7: Documentation

**Files:**
- Modify: `README.md` (a "Hosted trial (local)" section after the local-mode quick start)
- Modify: `CLAUDE.md` (a paragraph in "Hosted App libraries", after the 2a.4b paragraph, and the launcher's command list in "Commands")
- Modify: `HISTORY.md` (one entry at the top of "Unreleased")

**Interfaces:**
- Consumes: Tasks 1 to 6.
- Produces: nothing code depends on.

- [ ] **Step 1: The trial in the README**

After the local-mode quick start in `README.md`, add:

```markdown
## Hosted trial (local)

The hosted GitHub App runs on this machine's Docker for a trial: an org creates its own App, installs it, and gets reviews on repos whose contributors it trusts. No worker holds the org's provider credential. In the trial, pair state lives in memory, so restarting the control plane re-reviews every open PR once per persona, and a fresh session can re-post a finding it already posted. Keep the trial to installations you watch.

1. `./claudebox.sh hosted deploy --target local` builds both images and starts the control plane. It reports "not configured" until the next steps are done.
2. `./claudebox.sh app create --org ORG --target local` creates the App in your browser and writes its secrets into `hosted/claudebox.env`. It prints a checklist of what is left.
3. Add each profile's provider credential to `hosted/claudebox.env`, one `NAME=value` line each.
4. Write `admins.json`, for example `{"operators": [12345]}`: numeric GitHub user ids, never logins (`./claudebox.sh hosted whois LOGIN` prints one). Then run `./claudebox.sh hosted deploy --target local --admins admins.json`. Only this command changes who may administer the deployment, and running it again is how an admin is added or removed.
5. Tick Enable Device Flow in the App's settings (the checklist links the page), then run `./claudebox.sh hosted login`. It prints the id it logged you in as.
6. `./claudebox.sh hosted config apply claudebox.deploy.json`, then install the App on the repos to review.

`./claudebox.sh hosted status` shows what is running and what waits on a person, `hosted logs` follows the control plane, and `hosted stop` stops it and keeps its data. Nothing restarts the control plane after a host reboot: run `hosted deploy` again. Whoever can run Docker on this machine can also read the deployment's stores and its env file directly.
```

- [ ] **Step 2: Describe the surface in CLAUDE.md**

In `CLAUDE.md`'s "Hosted App libraries" subsection, after the paragraph about the control plane's processes, add:

```markdown
Phase 2a.5 is the operator surface. `admin.py` is the control plane's admin API, its own server on port 8082, never published on the host: the CLI reaches it over the deployment's Docker network. Every call but `GET /admin/v1/app` presents a GitHub user token, which counts only when GitHub's check-token call knows it as this App's (`github_app.user_for_app_token`, authenticated with the App's client id and secret), and the numeric id it names is matched against `CLAUDEBOX_ADMINS`, the role lists `hosted deploy --admins` writes. Logins are never matched. `status` and `config apply` need the `operators` role. `hosted_cli.py` is the in-image half of `app create`, `hosted whois`, `hosted login`, `hosted status` and `hosted config apply`. `claudebox.sh` runs it in a throwaway container of the control-plane image with `--entrypoint python3`, since that image's entrypoint is the supervisor, and takes from its stdout only what it keeps: the App's secrets go to the end of `hosted/claudebox.env`, and the user token to `~/.config/claudebox/hosted-local.token`, from which it crosses to later calls by name only. `hosted deploy` rewrites `CLAUDEBOX_CREDENTIALS` from the env file's own names on every run, and `compose up -d` recreates the container only when something changed, because every restart re-reviews every open PR.
```

In "Commands", after the `config validate` paragraph, add:

```markdown
`hosted deploy`, `app create`, `hosted whois`, `hosted login`, `hosted status`, `hosted config apply`, `hosted logs` and `hosted stop` are the hosted App's operator commands, for the local trial (README, "Hosted trial (local)"). Each command that takes options refuses the ones it does not, `config validate` included.
```

- [ ] **Step 3: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.5 of the hosted GitHub App: an org can run it.** `claudebox.sh hosted deploy`, `app create`, `hosted login`, `hosted status` and `hosted config apply` take a deployment from nothing to its first review on a laptop. Admins are numeric GitHub ids that only `hosted deploy --admins` can change, and a login counts only with a token the App itself issued. `config validate` now refuses an option it does not take, such as `--repo` where `--repo-name` was meant, rather than ignoring it.
```

- [ ] **Step 4: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && /bin/bash ./test-launcher.sh && /bin/bash ./test-worker.sh`
Expected: all six pass.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md HISTORY.md
git commit -m "docs: hosted phase 2a.5, the operator surface"
```
