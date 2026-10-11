# Hosted Phase 2a.1: Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** Six library modules under `hosted/` that the control plane, the gateway and the job runner are later built from: the deployment's cryptography, the records store with its single-instance lock, minted credentials, the encrypted blob store, the comment scan, and a GitHub App client. None of them runs a process.

**Architecture:** Flat modules imported by plain name, like `reviewer/`. `crypto.py` is the only module that imports a library (Debian's `python3-cryptography`, present only in the control-plane image). Everything else is standard library and takes its crypto, clock and network as arguments, so the host's test run exercises it with fakes. `records.py` creates schema version 1, every table 2a uses. `creds.py` stores salted hashes, never tokens. `blobs.py` seals each file with the deployment key and binds the seal to the blob's name. `scan.py` finds credential shapes and the deployment's own literals. `github_app.py` signs the App JWT, mints full and narrowed installation tokens, and makes the REST calls 2a needs, all through one transport seam.

**Tech Stack:** Python 3.9+ standard library (`sqlite3`, `fcntl`, `hashlib`, `hmac`, `urllib`, `unittest`); `cryptography` 38.0.4 (bookworm's `python3-cryptography`) in `crypto.py` only.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "The worker channel" (bootstrap, durable credentials), "Posting and the outbox" (the scan, the signature), "Secrets", "State" (the records store, the blob store, `secure_delete`), "Single instance, health, and deploys" (the lock), "Fork PRs", and "Creating the App" (the worker token's narrowing). The 2a decisions this plan binds are in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. The ones this plan needs are copied under Global Constraints, so an executor never needs that file.

**Prerequisite:** branch from `feat/hosted-phase1` (PR #17), or from `main` once it has merged.

## Global Constraints

- **Standard library only, except `hosted/crypto.py`**, which imports `cryptography` lazily, inside its classes (overview decision 4). No other module under `hosted/` may import it, and nothing under `reviewer/` changes in this plan.
- **Python 3.9 syntax.** `test-python.sh` runs on the host's Python 3.9. No `match`, no `X | Y` unions in runtime-evaluated annotations, no `datetime.UTC` (overview decision 15).
- **Flat modules, imported by plain name.** `tests/_path.py` puts `reviewer/` and then `hosted/` on `sys.path`. New tests are `tests/test_hosted_*.py`, discovered by the existing `test-python.sh` (overview decision 19).
- **The records schema is the overview's, verbatim.** Ten tables: `meta`, `deploy_config`, `credentials`, `jobs`, `token_records`, `check_runs`, `outbox`, `withheld`, `withheld_counts`, `audit`. Schema version 1. Times are integer Unix seconds, UTC.
- **Credential format**, from the overview: `cbx_<k>_<id>_<secret>`. `<k>` is `b`, `c` or `p`. `<id>` is 16 lowercase hex characters and is the row key. `<secret>` is 43 URL-safe base64 characters of 32 random bytes. The row stores a 16-byte salt and `sha256(salt || secret)`, and comparison uses `hmac.compare_digest`.
- The spec's bootstrap token: "single-use: the first `fetch job` exchanges it for the capability, and it expires five minutes after dispatch if unused."
- The spec's startup rule: "the successor revokes every outstanding row before it serves anything".
- The spec's records store: "SQLite on a volume for the local trial and Fly, with `secure_delete=ON`."
- The spec's blobs: "The control plane encrypts every blob with the deployment key before writing it." Transcript blob names are "self-describing immutable" ones (repo, PR, pair, timestamp).
- The spec's scan: it covers "known secret shapes and ... the literal value of every credential the deployment holds", and signing "Adds the `-claudebox` signature if missing".
- The spec's lock: "on SQLite, the file lock of a single writer". Here that is `fcntl.flock(LOCK_EX | LOCK_NB)` on a lock file (overview decision 13).
- The spec's worker token: "narrowed at mint time to contents, pull requests, issues and metadata, read only", and to the job's repository.
- The spec's fork rule: "The test is the head repository alone".
- Every existing suite (`test-python.sh`, `test-providers.sh`, `test-personas.sh`, `test-shim.sh`, `test-launcher.sh`) still passes.

## Decided here

The spec leaves these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **Blob seals are AES-256-GCM.** Each one has a random 12-byte nonce and a leading version byte (`0x01`), and the blob's name is the associated data. A sealed file renamed on disk then fails authentication instead of being read as another blob. The deployment key is standard base64 of 32 bytes, and anything else is refused by name.
2. **Blob names are lowercase segments** matching `[a-z0-9._-]+`, with `.` and `..` refused. A transcript is `transcripts/<owner>/<name>/pr-<n>/<mode>/<persona>/<yyyymmddthhmmssz>-<8 hex>.jsonl`. The random suffix keeps two commits in one second from colliding, which is what keeps names immutable.
3. **The scan's detectors** are: claudebox's own tokens, GitHub (`gh[pousr]_`, `github_pat_`), Anthropic (`sk-ant-`), OpenAI (`sk-`, `sk-proj-`), AWS access key ids, Slack (`xox` and one letter, so `xoxc-` and `xoxd-` too), Google API keys, and PEM private-key headers. There is no generic high-entropy rule, because reviewers quote commit SHAs and base64 fixtures all the time. Deployment literals shorter than 8 characters are skipped and named in `Scanner.skipped`, so the caller can warn about them.
4. **A redacted body** replaces every matched span with `[withheld:<detector>]`, and a literal's detector is `literal:<SECRET NAME>`. A value is never echoed.
5. **The fork rule fails closed.** A head repository GitHub reports as `null`, which is a deleted fork, counts as a fork.
6. **No credential leaves the API host by any route GitHub's answers control.** An installation client refreshes its token five minutes before expiry. A `Link: rel="next"` page off the API host is refused before any request is sent. The real transport follows no redirect at all: urllib would replay the `Authorization` header to wherever a 30x pointed, so a redirect comes back as the 30x it is and the caller treats it as an error. A repository name with a `.` or `..` segment is refused before it reaches a URL.
7. **Only `Retry-After`, an exhausted rate limit, a 5xx and an unreachable GitHub count as retryable.** That is the spec's "Only `Retry-After` and 5xx answers are retried". An exhausted primary rate limit (`x-ratelimit-remaining: 0`) is the same wait, timed from `x-ratelimit-reset`.
8. **Config files are read through the contents API with no `ref`**, which GitHub reads as the default branch. A file the API will not inline (over 1 MB) is an error that names its size. Phase 1's caps (64 KB and 256 KB) refuse it long before that.
9. **Every blob directory is 0700**, intermediate ones included, and a directory that already exists with looser permissions is tightened. `os.makedirs` applies its mode to the leaf alone, and a blob's directories name its repo.
10. **The rewrite test belongs to the control plane, through `Installation.is_ancestor`** (overview decision 16). It is the compare API's `ahead` or `identical` status, and a 404 counts as not an ancestor, so a rewrite that also dropped the old head still widens the scope.
11. **The gateway's `verify_against` accepts provider tokens only.** A token whose kind letter is not `p` is refused before its secret is compared.
## Review Focus

- **A token whose kind letter was edited** (`cbx_c_…` presented as `cbx_p_…`). Expected: refused, never resolved to the row by id alone. Pinned in Task 3.
- **Two workers racing one bootstrap token** (the job spec read from platform metadata by someone else). Expected: exactly one exchange succeeds. Pinned in Task 3.
- **A `Link` header or a 30x pointing off `api.github.com`.** Expected: refused or not followed, before the token leaves. Pinned in Task 6, the redirect case against real loopback servers.
- **A repo full name or login carrying path syntax** (`acme/web/../../x`, `../web`, `acme/..`, `octo/../x`). Expected: `ValueError` before any request. Pinned in Task 6.
- **A provider token presented to the gateway with its kind letter edited.** Expected: refused. Pinned in Task 3.
- **A blob file moved or renamed on disk.** Expected: it fails to open rather than serving another blob's content. Pinned in Task 4 (with a fake sealer that checks the associated data) and Task 1 (with the real cipher).
- **A records store written by a newer control plane** (a rollback). Expected: refused, naming the fix. Pinned in Task 2.

## Not run here

- `test_hosted_crypto.py`'s `SealerTest` and `SignerTest` skip on a host without `cryptography`, which is every host this suite normally runs on. A skip is not a pass. The Validator runs them under `cryptography==38.0.4` (bookworm's version) in a scratch virtualenv and records the result as evidence. From 2a.4 on, `hosted/Dockerfile` runs them at build time.

---

### Task 1: The path shim and the deployment's cryptography

**Files:**
- Modify: `tests/_path.py` (put `hosted/` on `sys.path` after `reviewer/`)
- Create: `hosted/crypto.py`
- Test: `tests/test_hosted_crypto.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `crypto.CryptoError(Exception)`.
  - `crypto.new_key() -> str` (standard base64 of 32 random bytes) and `crypto.decode_key(text: str) -> bytes` (exactly 32 bytes, surrounding whitespace ignored; `CryptoError` naming "not base64" or "must be 32 bytes; got N").
  - `crypto.AesGcmSealer(key: bytes)` with `seal(plaintext: bytes, aad: bytes) -> bytes` and `open(sealed: bytes, aad: bytes) -> bytes`. Sealed layout: `b"\x01" + nonce(12) + ciphertext+tag`. `open` raises `CryptoError` with "not a sealed blob" (wrong version or too short) or "blob failed authentication". Later tasks call this shape a **Sealer**.
  - `crypto.Rs256Signer(pem: bytes)` with `sign(message: bytes) -> bytes` (PKCS#1 v1.5, SHA-256). It refuses a non-PEM key ("not an unencrypted PEM key") and a non-RSA key ("not an RSA key"). Later tasks call this shape a **Signer**.
  - Module constants `KEY_BYTES = 32`, `NONCE_BYTES = 12`, `TAG_BYTES = 16`, `VERSION = b"\x01"`.

- [ ] **Step 1: Put `hosted/` on the test path**

Apply this change to `tests/_path.py` (the docstring's first line, and the one `sys.path.insert` line replaced by `_ROOT` and two inserts):

```diff
@@ -1,4 +1,4 @@
-"""Puts reviewer/ on sys.path, and guards the checkout against the test suite.
+"""Puts reviewer/ and hosted/ on sys.path, and guards the checkout against the test suite.
 
 Every test module imports this, which is the only reason the guard lives in a
 path shim: it is the one place the whole suite passes through, so it covers a
@@ -13,7 +13,11 @@ import os
 import stat
 import sys
 
-sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reviewer"))
+_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
+# hosted/ first, reviewer/ in front of it: a hosted module imports the decision
+# core by plain name, exactly as it does inside the control-plane image.
+sys.path.insert(0, os.path.join(_ROOT, "hosted"))
+sys.path.insert(0, os.path.join(_ROOT, "reviewer"))
 
 _CHECKOUT_GIT = os.path.join(
     os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".git"
```

- [ ] **Step 2: Write the failing test**

`tests/test_hosted_crypto.py`:

```python
import _path  # noqa: F401

import base64
import unittest

import crypto
from crypto import CryptoError, decode_key, new_key

try:
    import cryptography  # noqa: F401
    HAVE_LIB = True
except ImportError:
    # The host's test run has no cryptography; hosted/Dockerfile runs this
    # module at build time, where it does. A skip here is not a pass.
    HAVE_LIB = False


class KeyTest(unittest.TestCase):
    def test_a_new_key_decodes_to_32_bytes(self):
        self.assertEqual(len(decode_key(new_key())), 32)

    def test_two_new_keys_differ(self):
        self.assertNotEqual(new_key(), new_key())

    def test_surrounding_whitespace_is_ignored(self):
        key = new_key()
        self.assertEqual(decode_key(f"  {key}\n"), decode_key(key))

    def test_not_base64_is_refused_by_name(self):
        with self.assertRaises(CryptoError) as ctx:
            decode_key("not base64 at all!")
        self.assertIn("not base64", str(ctx.exception))

    def test_the_wrong_length_is_refused_by_name(self):
        with self.assertRaises(CryptoError) as ctx:
            decode_key(base64.b64encode(b"x" * 16).decode())
        self.assertIn("must be 32 bytes; got 16", str(ctx.exception))


@unittest.skipUnless(HAVE_LIB, "cryptography is not installed here; hosted/Dockerfile runs this")
class SealerTest(unittest.TestCase):
    def setUp(self):
        self.sealer = crypto.AesGcmSealer(b"k" * 32)

    def test_a_sealed_blob_opens_with_its_aad(self):
        sealed = self.sealer.seal(b"transcript", b"transcripts/a/b")
        self.assertEqual(self.sealer.open(sealed, b"transcripts/a/b"), b"transcript")

    def test_the_plaintext_does_not_appear_in_the_sealed_blob(self):
        self.assertNotIn(b"transcript", self.sealer.seal(b"transcript", b"n"))

    def test_sealing_twice_gives_different_blobs(self):
        self.assertNotEqual(self.sealer.seal(b"x", b"n"), self.sealer.seal(b"x", b"n"))

    def test_a_blob_moved_to_another_name_fails(self):
        sealed = self.sealer.seal(b"x", b"transcripts/a/b")
        with self.assertRaises(CryptoError) as ctx:
            self.sealer.open(sealed, b"transcripts/a/c")
        self.assertIn("failed authentication", str(ctx.exception))

    def test_a_flipped_byte_fails(self):
        sealed = bytearray(self.sealer.seal(b"payload", b"n"))
        sealed[-1] ^= 1
        with self.assertRaises(CryptoError):
            self.sealer.open(bytes(sealed), b"n")

    def test_another_key_cannot_open_it(self):
        sealed = self.sealer.seal(b"x", b"n")
        with self.assertRaises(CryptoError):
            crypto.AesGcmSealer(b"j" * 32).open(sealed, b"n")

    def test_an_unknown_version_byte_is_not_a_sealed_blob(self):
        sealed = b"\x02" + self.sealer.seal(b"x", b"n")[1:]
        with self.assertRaises(CryptoError) as ctx:
            self.sealer.open(sealed, b"n")
        self.assertIn("not a sealed blob", str(ctx.exception))

    def test_a_truncated_blob_is_not_a_sealed_blob(self):
        with self.assertRaises(CryptoError):
            self.sealer.open(b"\x01" + b"\x00" * 20, b"n")

    def test_a_short_key_is_refused(self):
        with self.assertRaises(CryptoError):
            crypto.AesGcmSealer(b"k" * 16)


@unittest.skipUnless(HAVE_LIB, "cryptography is not installed here; hosted/Dockerfile runs this")
class SignerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())

    def test_a_signature_verifies_against_the_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = crypto.Rs256Signer(self.pem).sign(b"header.payload")
        self.key.public_key().verify(sig, b"header.payload", padding.PKCS1v15(), hashes.SHA256())

    def test_a_pem_that_is_not_a_key_is_refused_by_name(self):
        with self.assertRaises(CryptoError) as ctx:
            crypto.Rs256Signer(b"-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")
        self.assertIn("not an unencrypted PEM key", str(ctx.exception))

    def test_an_ec_key_is_refused_by_name(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        with self.assertRaises(CryptoError) as ctx:
            crypto.Rs256Signer(pem)
        self.assertIn("not an RSA key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_crypto.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'crypto'`.

- [ ] **Step 4: Implement**

`hosted/crypto.py`:

```python
"""The deployment's cryptography, behind the one module that imports a library for it.

The standard library has neither RS256, which a GitHub App JWT needs, nor an
AEAD cipher, which encrypting blobs with the deployment key needs. Both come
from Debian's python3-cryptography, installed in the control-plane image only.
reviewer/ and the worker stay standard-library only.

The library is imported inside the classes, not at module level, so the host's
test run, which does not have it, can still import this module for decode_key
and new_key. Everything else that seals or signs takes a Sealer or a Signer
argument, and the host tests hand it a fake.
"""

import base64
import binascii
import os

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
# The first byte of every sealed blob. A future cipher change bumps it, so an
# old blob is refused by name instead of failing authentication.
VERSION = b"\x01"


class CryptoError(Exception):
    """A key that cannot be used, or a blob that is not ours or was altered."""


def new_key() -> str:
    """A fresh deployment key, base64, for setup to write into the secret store."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def decode_key(text: str) -> bytes:
    """The deployment key from its secret-store form.

    Standard base64 of exactly 32 bytes. Anything else is refused by name, since
    a key that decodes to the wrong length would otherwise surface later as a
    library error nobody connects with the secret store.
    """
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError("the deployment key is not base64") from None
    if len(raw) != KEY_BYTES:
        raise CryptoError(f"the deployment key must be {KEY_BYTES} bytes; got {len(raw)}")
    return raw


class AesGcmSealer:
    """AES-256-GCM with a random nonce per blob.

    `aad` binds a blob to its name: a sealed file moved to another name fails
    authentication instead of being read as that name's content.
    """

    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise CryptoError(f"the deployment key must be {KEY_BYTES} bytes; got {len(key)}")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return VERSION + nonce + self._aead.encrypt(nonce, plaintext, aad)

    def open(self, sealed: bytes, aad: bytes) -> bytes:
        if len(sealed) < 1 + NONCE_BYTES + TAG_BYTES or sealed[:1] != VERSION:
            raise CryptoError("not a sealed blob")
        from cryptography.exceptions import InvalidTag
        nonce = sealed[1:1 + NONCE_BYTES]
        try:
            return self._aead.decrypt(nonce, sealed[1 + NONCE_BYTES:], aad)
        except InvalidTag:
            raise CryptoError("blob failed authentication") from None


class Rs256Signer:
    """RSASSA-PKCS1-v1_5 with SHA-256 over the App's private key, for its JWT."""

    def __init__(self, pem: bytes):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        try:
            key = load_pem_private_key(pem, password=None)
        except (ValueError, TypeError):
            raise CryptoError("the App private key is not an unencrypted PEM key") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise CryptoError("the App private key is not an RSA key")
        self._key = key

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())
```

- [ ] **Step 5: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_crypto.py'`, then `./test-python.sh`
Expected: PASS on the host with `skipped=12` (the `KeyTest` cases run). Under a Python with `cryptography==38.0.4`, PASS with nothing skipped. The whole suite passes.

- [ ] **Step 6: Commit**

```bash
git add tests/_path.py hosted/crypto.py tests/test_hosted_crypto.py
git commit -m "hosted: the deployment's cryptography, behind one module"
```

### Task 2: The records store and the single-instance lock

**Files:**
- Create: `hosted/records.py`
- Test: `tests/test_hosted_records.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `records.SCHEMA_VERSION = 1`, `records.RecordsError(Exception)`.
  - `records.Records(path: str)`: opens or creates the store. A store at a newer version raises `RecordsError` naming "newer than" and "deploy the newer image". An older one raises naming "no migration".
  - `Records.conn() -> sqlite3.Connection`: this thread's connection (`row_factory = sqlite3.Row`, WAL, `secure_delete=ON`, `foreign_keys=ON`, `busy_timeout=5000`, autocommit).
  - `Records.tx()`: a context manager yielding the connection inside `BEGIN IMMEDIATE`. It commits on exit and rolls back and re-raises on any exception.
  - `Records.close()` closes this thread's connection.
  - `records.InstanceLock(path: str)` with `try_acquire() -> bool`, `release()` and the `held` property. The lock file is created mode 600.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_records.py`:

```python
import _path  # noqa: F401

import os
import sqlite3
import tempfile
import threading
import unittest

from records import SCHEMA_VERSION, InstanceLock, Records, RecordsError

TABLES = {"meta", "deploy_config", "credentials", "jobs", "token_records", "check_runs",
          "outbox", "withheld", "withheld_counts", "audit"}


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "records.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_new_store_has_every_table_and_version_1(self):
        r = Records(self.path)
        names = {row[0] for row in r.conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(names, TABLES)
        version = r.conn().execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertEqual(version, "1")
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_reopening_keeps_the_rows(self):
        r = Records(self.path)
        with r.tx() as c:
            c.execute("INSERT INTO audit (at, action, scope, detail) VALUES (1, 'a', 's', 'd')")
        r.close()
        self.assertEqual(
            Records(self.path).conn().execute("SELECT count(*) FROM audit").fetchone()[0], 1)

    def test_secure_delete_wal_and_foreign_keys_are_on(self):
        c = Records(self.path).conn()
        self.assertEqual(c.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_an_exception_in_a_transaction_rolls_it_back(self):
        r = Records(self.path)
        with self.assertRaises(ZeroDivisionError):
            with r.tx() as c:
                c.execute("INSERT INTO audit (at, action, scope, detail) VALUES (1, 'a', 's', 'd')")
                1 / 0
        self.assertEqual(r.conn().execute("SELECT count(*) FROM audit").fetchone()[0], 0)

    def test_a_newer_schema_is_refused_naming_the_fix(self):
        Records(self.path).close()
        c = sqlite3.connect(self.path)
        c.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        c.commit()
        c.close()
        with self.assertRaises(RecordsError) as ctx:
            Records(self.path)
        self.assertIn("schema version 2, newer than", str(ctx.exception))
        self.assertIn("deploy the newer image", str(ctx.exception))

    def test_an_older_schema_is_refused(self):
        Records(self.path).close()
        c = sqlite3.connect(self.path)
        c.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        c.commit()
        c.close()
        with self.assertRaises(RecordsError) as ctx:
            Records(self.path)
        self.assertIn("no migration", str(ctx.exception))

    def test_each_thread_gets_its_own_connection(self):
        r = Records(self.path)
        seen = []
        t = threading.Thread(target=lambda: seen.append(r.conn()))
        t.start()
        t.join()
        self.assertIsNot(seen[0], r.conn())

    def test_the_check_constraint_refuses_an_unknown_credential_kind(self):
        r = Records(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            with r.tx() as c:
                c.execute("INSERT INTO credentials (id, kind, salt, hash, job_id, expires_at) "
                          "VALUES ('x', 'admin', x'00', x'00', 'j', 1)")


class InstanceLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "control.lock")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_first_holder_gets_it_and_a_second_is_refused(self):
        first, second = InstanceLock(self.path), InstanceLock(self.path)
        self.assertTrue(first.try_acquire())
        self.assertFalse(second.try_acquire())
        self.assertTrue(first.held)
        self.assertFalse(second.held)
        first.release()

    def test_a_released_lock_can_be_taken(self):
        first, second = InstanceLock(self.path), InstanceLock(self.path)
        first.try_acquire()
        first.release()
        self.assertTrue(second.try_acquire())
        second.release()

    def test_acquiring_twice_is_harmless(self):
        lock = InstanceLock(self.path)
        self.assertTrue(lock.try_acquire())
        self.assertTrue(lock.try_acquire())
        lock.release()
        self.assertFalse(lock.held)

    def test_the_lock_file_is_mode_600(self):
        lock = InstanceLock(self.path)
        lock.try_acquire()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        lock.release()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_records.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'records'`.

- [ ] **Step 3: Implement**

`hosted/records.py`:

```python
"""The records store: SQLite, one file on the deployment's volume.

Everything durable that is not a blob lives here. 2a creates every table it
uses (schema version 1); 2b's pair-state tables arrive as version 2. Times are
integer Unix seconds, UTC.

One connection per thread, because sqlite3 connections must not cross threads.
Writes go through tx(), which takes SQLite's write lock up front (BEGIN
IMMEDIATE) so two writers queue instead of one failing halfway through.
`secure_delete` is on so a deleted row's bytes are overwritten, which is part
of what makes erasure erase.
"""

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE deploy_config (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      body BLOB NOT NULL, applied_at INTEGER NOT NULL, applied_by INTEGER NOT NULL)""",
    """CREATE TABLE credentials (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL CHECK (kind IN ('bootstrap', 'capability', 'provider')),
      salt BLOB NOT NULL, hash BLOB NOT NULL,
      job_id TEXT NOT NULL, persona TEXT,
      expires_at INTEGER NOT NULL, used_at INTEGER, revoked_at INTEGER)""",
    "CREATE INDEX credentials_job ON credentials (job_id)",
    """CREATE TABLE jobs (
      id TEXT PRIMARY KEY, installation_id INTEGER NOT NULL, repo TEXT NOT NULL,
      pr INTEGER NOT NULL, head TEXT NOT NULL, mode TEXT NOT NULL,
      profile TEXT NOT NULL, model TEXT NOT NULL,
      dispatched_at INTEGER NOT NULL, fetched_at INTEGER, deadline_at INTEGER,
      ended_at INTEGER, end_reason TEXT)""",
    """CREATE TABLE token_records (
      credential_id TEXT PRIMARY KEY REFERENCES credentials (id),
      seq INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
      successes INTEGER NOT NULL DEFAULT 0,
      input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
      first_limit TEXT, first_failure TEXT, first_rejected TEXT, cap_hit TEXT,
      usage_missing INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE check_runs (
      repo TEXT NOT NULL, head TEXT NOT NULL, run_id INTEGER NOT NULL,
      posted INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
      withheld INTEGER NOT NULL DEFAULT 0, not_posted INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL, PRIMARY KEY (repo, head))""",
    """CREATE TABLE outbox (
      key TEXT PRIMARY KEY,
      installation_id INTEGER NOT NULL, repo TEXT NOT NULL, pr INTEGER NOT NULL,
      head TEXT NOT NULL, body TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL)""",
    "CREATE INDEX outbox_due ON outbox (installation_id, next_attempt_at)",
    """CREATE TABLE withheld (
      id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL,
      job_id TEXT NOT NULL, persona TEXT NOT NULL, detector TEXT NOT NULL,
      salted_hash BLOB NOT NULL, redacted_body TEXT NOT NULL, created_at INTEGER NOT NULL)""",
    """CREATE TABLE withheld_counts (
      detector TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
      PRIMARY KEY (detector, day))""",
    """CREATE TABLE audit (
      id INTEGER PRIMARY KEY, at INTEGER NOT NULL, actor_id INTEGER,
      action TEXT NOT NULL, scope TEXT NOT NULL, detail TEXT NOT NULL)""",
)


class RecordsError(Exception):
    """A records store this control plane cannot use."""


class Records:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init_schema()

    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None: the module's implicit transactions are off,
            # and tx() says BEGIN and COMMIT itself.
            c = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA secure_delete=ON")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. Any exception rolls it back and propagates."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self.tx() as c:
            has_meta = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()
            if not has_meta:
                for statement in _SCHEMA:
                    c.execute(statement)
                c.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                          (str(SCHEMA_VERSION),))
                return
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RecordsError(
                f"the records store is schema version {version}, newer than this "
                f"control plane's {SCHEMA_VERSION}; deploy the newer image")
        if version < SCHEMA_VERSION:
            raise RecordsError(
                f"the records store is schema version {version} and this control plane "
                f"has no migration from it to {SCHEMA_VERSION}")


class InstanceLock:
    """The single-instance lock: an exclusive flock on a file beside the store.

    SQLite's own locks last one transaction, so a lock file is how one process
    holds the store between transactions. flock locks belong to the open file,
    so a second InstanceLock on the same path is refused even in this process,
    which is what lets a test exercise the refusal without a second process.
    The lock dies with the process, which is what a crashed instance needs.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_records.py'`, then `./test-python.sh`
Expected: PASS, 12 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/records.py tests/test_hosted_records.py
git commit -m "hosted: the records store, schema version 1, and the instance lock"
```

### Task 3: Credentials: bootstrap tokens, capabilities, provider tokens

**Files:**
- Create: `hosted/creds.py`
- Test: `tests/test_hosted_creds.py`

**Interfaces:**
- Consumes: `records.Records` (Task 2).
- Produces:
  - `creds.KINDS = {"bootstrap": "b", "capability": "c", "provider": "p"}`, `creds.TOKEN_RE`, `creds.BOOTSTRAP_TTL = 300`.
  - `creds.Credential(id: str, kind: str, job_id: str, persona: Optional[str], expires_at: int)`, frozen.
  - `creds.Creds(records: Records, clock: Callable[[], float] = time.time)` with:
    - `mint(kind, job_id, ttl, persona=None) -> str`: a provider token must name a persona and nothing else may; an unknown kind or a ttl of 0 or less raises `ValueError`.
    - `check(token, kind) -> Optional[Credential]`: never raises on a malformed token. A credential is dead from `expires_at` on, once revoked, and, for a bootstrap token, once used.
    - `exchange_bootstrap(token, capability_ttl) -> Optional[Tuple[str, Credential]]`: marks the bootstrap token used and mints the job's capability in one transaction.
    - `revoke(credential_id)`, `revoke_job(job_id) -> int`, `revoke_all() -> int`.
    - `live_provider_rows() -> List[dict]`: keys `credential_id`, `salt_hex`, `hash_hex`, `job_id`, `persona`, `expires_at`.
  - `creds.verify_against(token, salt_hex, hash_hex) -> bool` (provider tokens only: any other kind letter is refused) and `creds.credential_id_of(token) -> Optional[str]`, for the gateway (2a.2), which holds rows rather than a records store.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_creds.py`:

```python
import _path  # noqa: F401

import os
import tempfile
import threading
import unittest

from creds import BOOTSTRAP_TTL, TOKEN_RE, Creds, credential_id_of, verify_against
from records import Records


class Clock:
    def __init__(self, now=1_000_000):
        self.now = now

    def __call__(self):
        return self.now


class CredsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.records = Records(os.path.join(self.dir.name, "records.db"))
        self.clock = Clock()
        self.creds = Creds(self.records, self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_minted_token_has_the_documented_shape(self):
        token = self.creds.mint("capability", "job1", 60)
        self.assertRegex(token, r"^cbx_c_[0-9a-f]{16}_[A-Za-z0-9_-]{43}$")
        self.assertTrue(TOKEN_RE.fullmatch(token))

    def test_the_store_holds_no_token(self):
        token = self.creds.mint("capability", "job1", 60)
        secret = token.rsplit("_", 1)[1]
        dump = "\n".join(self.records.conn().iterdump())
        self.assertNotIn(secret, dump)

    def test_check_returns_the_credential(self):
        token = self.creds.mint("provider", "job1", 60, persona="sage")
        cred = self.creds.check(token, "provider")
        self.assertEqual((cred.kind, cred.job_id, cred.persona), ("provider", "job1", "sage"))
        self.assertEqual(cred.expires_at, 1_000_060)

    def test_a_token_of_another_kind_is_refused(self):
        token = self.creds.mint("capability", "job1", 60)
        self.assertIsNone(self.creds.check(token, "provider"))

    def test_a_token_with_its_letter_changed_is_refused(self):
        token = self.creds.mint("capability", "job1", 60)
        self.assertIsNone(self.creds.check("cbx_p_" + token[6:], "provider"))

    def test_a_wrong_secret_is_refused(self):
        token = self.creds.mint("capability", "job1", 60)
        last = "A" if token[-1] != "A" else "B"
        self.assertIsNone(self.creds.check(token[:-1] + last, "capability"))

    def test_garbage_is_refused_without_raising(self):
        for junk in ("", "Bearer x", "cbx_c_zz_", None):
            with self.subTest(junk=junk):
                self.assertIsNone(self.creds.check(junk, "capability"))

    def test_an_expired_token_is_refused_at_its_expiry(self):
        token = self.creds.mint("capability", "job1", 60)
        self.clock.now += 59
        self.assertIsNotNone(self.creds.check(token, "capability"))
        self.clock.now += 1
        self.assertIsNone(self.creds.check(token, "capability"))

    def test_a_revoked_token_is_refused(self):
        token = self.creds.mint("capability", "job1", 60)
        self.creds.revoke(credential_id_of(token))
        self.assertIsNone(self.creds.check(token, "capability"))

    def test_revoke_job_revokes_only_that_job(self):
        a = self.creds.mint("capability", "job1", 60)
        b = self.creds.mint("provider", "job1", 60, persona="sage")
        c = self.creds.mint("capability", "job2", 60)
        self.assertEqual(self.creds.revoke_job("job1"), 2)
        self.assertIsNone(self.creds.check(a, "capability"))
        self.assertIsNone(self.creds.check(b, "provider"))
        self.assertIsNotNone(self.creds.check(c, "capability"))

    def test_revoke_all_revokes_everything_outstanding(self):
        tokens = [self.creds.mint("capability", f"job{i}", 60) for i in range(3)]
        self.assertEqual(self.creds.revoke_all(), 3)
        self.assertEqual(self.creds.revoke_all(), 0)
        for t in tokens:
            self.assertIsNone(self.creds.check(t, "capability"))

    def test_revocation_survives_reopening_the_store(self):
        token = self.creds.mint("capability", "job1", 60)
        self.creds.revoke_all()
        self.records.close()
        again = Creds(Records(self.records.path), self.clock)
        self.assertIsNone(again.check(token, "capability"))

    def test_a_bootstrap_token_exchanges_once_for_a_capability(self):
        boot = self.creds.mint("bootstrap", "job1", BOOTSTRAP_TTL)
        cap, cred = self.creds.exchange_bootstrap(boot, 3300)
        self.assertEqual((cred.kind, cred.job_id), ("capability", "job1"))
        self.assertIsNotNone(self.creds.check(cap, "capability"))
        self.assertIsNone(self.creds.exchange_bootstrap(boot, 3300))
        self.assertIsNone(self.creds.check(boot, "bootstrap"))

    def test_an_unused_bootstrap_token_dies_after_five_minutes(self):
        self.assertEqual(BOOTSTRAP_TTL, 300)
        boot = self.creds.mint("bootstrap", "job1", BOOTSTRAP_TTL)
        self.clock.now += 300
        self.assertIsNone(self.creds.exchange_bootstrap(boot, 3300))

    def test_racing_exchanges_succeed_exactly_once(self):
        boot = self.creds.mint("bootstrap", "job1", BOOTSTRAP_TTL)
        results = []
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            results.append(self.creds.exchange_bootstrap(boot, 3300))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_a_provider_token_must_name_a_persona_and_nothing_else_may(self):
        with self.assertRaises(ValueError):
            self.creds.mint("provider", "job1", 60)
        with self.assertRaises(ValueError):
            self.creds.mint("capability", "job1", 60, persona="sage")

    def test_an_unknown_kind_or_a_non_positive_ttl_is_refused(self):
        with self.assertRaises(ValueError):
            self.creds.mint("admin", "job1", 60)
        with self.assertRaises(ValueError):
            self.creds.mint("capability", "job1", 0)

    def test_live_provider_rows_verify_the_token_they_came_from(self):
        token = self.creds.mint("provider", "job1", 60, persona="sage")
        self.creds.mint("capability", "job1", 60)
        rows = self.creds.live_provider_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["job_id"], row["persona"]), ("job1", "sage"))
        self.assertEqual(row["credential_id"], credential_id_of(token))
        self.assertTrue(verify_against(token, row["salt_hex"], row["hash_hex"]))
        self.assertFalse(verify_against(token[:-1] + ("A" if token[-1] != "A" else "B"),
                                        row["salt_hex"], row["hash_hex"]))

    def test_verify_against_refuses_a_provider_token_with_its_kind_letter_edited(self):
        token = self.creds.mint("provider", "job1", 60, persona="sage")
        row = self.creds.live_provider_rows()[0]
        for letter in ("b", "c"):
            with self.subTest(letter=letter):
                edited = f"cbx_{letter}_" + token[6:]
                self.assertFalse(verify_against(edited, row["salt_hex"], row["hash_hex"]))

    def test_live_provider_rows_leave_out_revoked_and_expired_tokens(self):
        a = self.creds.mint("provider", "job1", 60, persona="sage")
        self.creds.mint("provider", "job2", 10, persona="sme")
        self.creds.revoke(credential_id_of(a))
        self.clock.now += 10
        self.assertEqual(self.creds.live_provider_rows(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_creds.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'creds'`.

- [ ] **Step 3: Implement**

`hosted/creds.py`:

```python
"""Every credential claudebox mints: bootstrap tokens, capabilities, provider tokens.

A credential is a row holding a salted hash, never the token itself, so a copy
of the records store mints nothing. Each one has the form
cbx_<k>_<id>_<secret>: <k> is b, c or p for its kind, <id> is the row's key,
and <secret> is 32 random bytes in URL-safe base64. The id makes lookup one
indexed read, and the prefix gives the scanner a shape to find a leaked one by.

A bootstrap token is single-use: exchange_bootstrap marks it used in the same
transaction that mints the job's capability, so two racing exchanges cannot
both succeed. revoke_all is what a control-plane instance runs at startup,
before it serves anything, so an orphaned worker's next call fails.
"""

import base64
import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from records import Records

KINDS = {"bootstrap": "b", "capability": "c", "provider": "p"}
_KIND_OF_LETTER = {letter: kind for kind, letter in KINDS.items()}
TOKEN_RE = re.compile(r"cbx_([bcp])_([0-9a-f]{16})_([A-Za-z0-9_-]{43})")
# The spec's "it expires five minutes after dispatch if unused".
BOOTSTRAP_TTL = 300


@dataclass(frozen=True)
class Credential:
    id: str
    kind: str
    job_id: str
    persona: Optional[str]
    expires_at: int


def _hash(salt: bytes, secret: str) -> bytes:
    return hashlib.sha256(salt + secret.encode("ascii")).digest()


class Creds:
    def __init__(self, records: Records, clock: Callable[[], float] = time.time):
        self.records = records
        self.clock = clock

    def _now(self) -> int:
        return int(self.clock())

    def _insert(self, c, kind: str, job_id: str, ttl: int, persona: Optional[str]) -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown credential kind {kind!r}")
        if (kind == "provider") != (persona is not None):
            raise ValueError("a provider token names its persona, and nothing else does")
        if ttl <= 0:
            raise ValueError("a credential's ttl must be positive")
        cred_id = os.urandom(8).hex()
        secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        salt = os.urandom(16)
        c.execute(
            "INSERT INTO credentials (id, kind, salt, hash, job_id, persona, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cred_id, kind, salt, _hash(salt, secret), job_id, persona, self._now() + ttl))
        return f"cbx_{KINDS[kind]}_{cred_id}_{secret}"

    def mint(self, kind: str, job_id: str, ttl: int, persona: Optional[str] = None) -> str:
        """A new credential's token. The token is returned once and stored nowhere."""
        with self.records.tx() as c:
            return self._insert(c, kind, job_id, ttl, persona)

    def _verify(self, c, token: str, kind: str) -> Optional[Credential]:
        m = TOKEN_RE.fullmatch(token or "")
        if not m or _KIND_OF_LETTER[m.group(1)] != kind:
            return None
        row = c.execute("SELECT * FROM credentials WHERE id = ?", (m.group(2),)).fetchone()
        if row is None or row["kind"] != kind or row["revoked_at"] is not None:
            return None
        if self._now() >= row["expires_at"]:
            return None
        if kind == "bootstrap" and row["used_at"] is not None:
            return None
        if not hmac.compare_digest(bytes(row["hash"]), _hash(bytes(row["salt"]), m.group(3))):
            return None
        return Credential(row["id"], row["kind"], row["job_id"], row["persona"], row["expires_at"])

    def check(self, token: str, kind: str) -> Optional[Credential]:
        """The live credential this token is, or None. Never raises on a bad token."""
        return self._verify(self.records.conn(), token, kind)

    def exchange_bootstrap(self, token: str, capability_ttl: int) -> Optional[Tuple[str, Credential]]:
        """Spend a bootstrap token on its job's capability. None if it is not live."""
        with self.records.tx() as c:
            boot = self._verify(c, token, "bootstrap")
            if boot is None:
                return None
            c.execute("UPDATE credentials SET used_at = ? WHERE id = ?", (self._now(), boot.id))
            cap = self._insert(c, "capability", boot.job_id, capability_ttl, None)
        return cap, self.check(cap, "capability")

    def revoke(self, credential_id: str) -> None:
        with self.records.tx() as c:
            c.execute("UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                      (self._now(), credential_id))

    def revoke_job(self, job_id: str) -> int:
        """Revoke every live credential of one job. Returns how many."""
        with self.records.tx() as c:
            return c.execute(
                "UPDATE credentials SET revoked_at = ? WHERE job_id = ? AND revoked_at IS NULL",
                (self._now(), job_id)).rowcount

    def revoke_all(self) -> int:
        """Revoke every outstanding credential. Run at startup, before serving."""
        with self.records.tx() as c:
            return c.execute("UPDATE credentials SET revoked_at = ? WHERE revoked_at IS NULL",
                             (self._now(),)).rowcount

    def live_provider_rows(self) -> List[dict]:
        """Each unrevoked, unexpired provider token's row, hash and salt included.

        What the control plane hands a starting gateway, so the gateway can check
        a presented token itself without the token ever being stored.
        """
        rows = self.records.conn().execute(
            "SELECT id, salt, hash, job_id, persona, expires_at FROM credentials "
            "WHERE kind = 'provider' AND revoked_at IS NULL AND expires_at > ?",
            (self._now(),)).fetchall()
        return [{"credential_id": r["id"], "salt_hex": bytes(r["salt"]).hex(),
                 "hash_hex": bytes(r["hash"]).hex(), "job_id": r["job_id"],
                 "persona": r["persona"], "expires_at": r["expires_at"]} for r in rows]


def verify_against(token: str, salt_hex: str, hash_hex: str) -> bool:
    """Check a presented provider token against a row the gateway was handed.

    The gateway holds no records store; it is given rows by the control plane,
    and only provider rows. A token whose kind letter is not `p` is refused
    before its secret is compared, so a capability with its letter edited
    never passes as a provider token.
    """
    m = TOKEN_RE.fullmatch(token or "")
    if not m or m.group(1) != KINDS["provider"]:
        return False
    return hmac.compare_digest(bytes.fromhex(hash_hex), _hash(bytes.fromhex(salt_hex), m.group(3)))


def credential_id_of(token: str) -> Optional[str]:
    """The row id inside a well-formed token, or None."""
    m = TOKEN_RE.fullmatch(token or "")
    return m.group(2) if m else None
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_creds.py'`, then `./test-python.sh`
Expected: PASS, 20 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/creds.py tests/test_hosted_creds.py
git commit -m "hosted: credentials as salted-hash rows, single-use bootstrap, revoke all"
```

### Task 4: The encrypted blob store

**Files:**
- Create: `hosted/blobs.py`
- Test: `tests/test_hosted_blobs.py`

**Interfaces:**
- Consumes: a Sealer (Task 1's `AesGcmSealer` shape: `seal(plaintext, aad)`, `open(sealed, aad)`). The tests pass a fake one.
- Produces:
  - `blobs.BlobNameError(ValueError)` and `blobs.check_name(name) -> str`.
  - `blobs.transcript_name(repo: str, pr: int, mode: str, persona: str, now: float) -> str`.
  - `blobs.transcripts_prefix(repo: Optional[str] = None, pr: Optional[int] = None) -> str`.
  - `blobs.Blobs(root: str, sealer)` with `put(name, data: bytes)` (atomic, files 600, every store directory 700 and tightened if it already exists), `get(name) -> Optional[bytes]`, `delete(name) -> bool`, `list(prefix) -> List[str]` (sorted, temporary files excluded) and `delete_prefix(prefix) -> int`. The seal's associated data is `name.encode("utf-8")`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_blobs.py`:

```python
import _path  # noqa: F401

import os
import tempfile
import unittest

from blobs import BlobNameError, Blobs, check_name, transcript_name, transcripts_prefix


class FakeSealer:
    """Reversible and AAD-checking, so the store's use of both is observable."""

    def seal(self, plaintext, aad):
        return b"SEALED:" + aad + b":" + plaintext[::-1]

    def open(self, sealed, aad):
        prefix = b"SEALED:" + aad + b":"
        if not sealed.startswith(prefix):
            raise ValueError("aad mismatch")
        return sealed[len(prefix):][::-1]


class NamesTest(unittest.TestCase):
    def test_a_transcript_name_says_what_it_is(self):
        name = transcript_name("Acme/Web", 12, "code", "red_team", 0)
        self.assertRegex(name, r"^transcripts/acme/web/pr-12/code/red_team/"
                               r"19700101t000000z-[0-9a-f]{8}\.jsonl$")

    def test_two_transcript_names_in_one_second_differ(self):
        self.assertNotEqual(transcript_name("a/b", 1, "code", "sage", 0),
                            transcript_name("a/b", 1, "code", "sage", 0))

    def test_prefixes_narrow_from_all_to_repo_to_pr(self):
        self.assertEqual(transcripts_prefix(), "transcripts")
        self.assertEqual(transcripts_prefix("Acme/Web"), "transcripts/acme/web")
        self.assertEqual(transcripts_prefix("acme/web", 3), "transcripts/acme/web/pr-3")

    def test_names_that_could_escape_are_refused(self):
        for bad in ("", "/etc/passwd", "a/../b", "a//b", "a/./b", "A/b", "a/b c",
                    "a/x.tmp-1234"):
            with self.subTest(bad=bad), self.assertRaises(BlobNameError):
                check_name(bad)

    def test_a_dot_github_repo_is_a_valid_segment(self):
        self.assertEqual(check_name("transcripts/acme/.github/pr-1"),
                         "transcripts/acme/.github/pr-1")


class BlobsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, "blobs")
        self.blobs = Blobs(self.root, FakeSealer())

    def tearDown(self):
        self.dir.cleanup()

    def test_put_then_get_round_trips(self):
        self.blobs.put("jobs/j1/material.json", b"{}")
        self.assertEqual(self.blobs.get("jobs/j1/material.json"), b"{}")

    def test_what_lands_on_disk_is_sealed_with_the_name(self):
        self.blobs.put("jobs/j1/material.json", b"secret prompt")
        with open(os.path.join(self.root, "jobs", "j1", "material.json"), "rb") as f:
            raw = f.read()
        self.assertNotIn(b"secret prompt", raw)
        self.assertTrue(raw.startswith(b"SEALED:jobs/j1/material.json:"))

    def test_a_file_moved_to_another_name_does_not_open(self):
        self.blobs.put("jobs/j1/material.json", b"x")
        os.makedirs(os.path.join(self.root, "jobs", "j2"))
        os.rename(os.path.join(self.root, "jobs", "j1", "material.json"),
                  os.path.join(self.root, "jobs", "j2", "material.json"))
        with self.assertRaises(ValueError):
            self.blobs.get("jobs/j2/material.json")

    def test_a_missing_blob_is_none(self):
        self.assertIsNone(self.blobs.get("jobs/nope/material.json"))

    def test_put_replaces_and_leaves_no_temporary_file(self):
        self.blobs.put("jobs/j1/material.json", b"one")
        self.blobs.put("jobs/j1/material.json", b"two")
        self.assertEqual(self.blobs.get("jobs/j1/material.json"), b"two")
        self.assertEqual(os.listdir(os.path.join(self.root, "jobs", "j1")), ["material.json"])

    def test_a_failed_seal_leaves_nothing_behind(self):
        class Broken(FakeSealer):
            def seal(self, plaintext, aad):
                raise RuntimeError("boom")
        blobs = Blobs(self.root, Broken())
        with self.assertRaises(RuntimeError):
            blobs.put("jobs/j1/material.json", b"x")
        self.assertEqual(os.listdir(os.path.join(self.root, "jobs", "j1")), [])

    def test_files_and_directories_are_private(self):
        self.blobs.put("jobs/j1/material.json", b"x")
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.root, "jobs", "j1")).st_mode & 0o777, 0o700)
        self.assertEqual(
            os.stat(os.path.join(self.root, "jobs", "j1", "material.json")).st_mode & 0o777, 0o600)

    def test_every_intermediate_directory_is_private(self):
        name = transcript_name("acme/web", 1, "code", "sage", 0)
        self.blobs.put(name, b"t")
        directory = self.root
        for segment in name.split("/")[:-1]:
            directory = os.path.join(directory, segment)
            with self.subTest(directory=directory):
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_permissive_existing_directories_are_tightened(self):
        os.chmod(self.root, 0o755)
        os.makedirs(os.path.join(self.root, "jobs"), mode=0o755)
        os.chmod(os.path.join(self.root, "jobs"), 0o755)
        again = Blobs(self.root, FakeSealer())
        again.put("jobs/j1/material.json", b"x")
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.root, "jobs")).st_mode & 0o777, 0o700)

    def test_list_and_delete_by_prefix(self):
        a = transcript_name("acme/web", 1, "code", "sage", 0)
        b = transcript_name("acme/web", 2, "code", "sage", 0)
        c = transcript_name("acme/api", 1, "code", "sage", 0)
        for n in (a, b, c):
            self.blobs.put(n, b"t")
        self.assertEqual(self.blobs.list(transcripts_prefix("acme/web")), sorted([a, b]))
        self.assertEqual(self.blobs.list(transcripts_prefix("acme/web", 1)), [a])
        self.assertEqual(self.blobs.delete_prefix(transcripts_prefix("acme/web")), 2)
        self.assertEqual(self.blobs.list(transcripts_prefix()), [c])

    def test_list_of_an_absent_prefix_is_empty(self):
        self.assertEqual(self.blobs.list("transcripts/nobody"), [])

    def test_list_skips_a_stranded_temporary_file(self):
        self.blobs.put("jobs/j1/material.json", b"x")
        open(os.path.join(self.root, "jobs", "j1", "material.json.tmp-dead"), "wb").close()
        self.assertEqual(self.blobs.list("jobs"), ["jobs/j1/material.json"])

    def test_delete_reports_whether_anything_was_there(self):
        self.blobs.put("jobs/j1/material.json", b"x")
        self.assertTrue(self.blobs.delete("jobs/j1/material.json"))
        self.assertFalse(self.blobs.delete("jobs/j1/material.json"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_blobs.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'blobs'`.

- [ ] **Step 3: Implement**

`hosted/blobs.py`:

```python
"""The blob store: one directory, every file sealed with the deployment key.

A blob's name says what it is (repo, PR, pair, time), so erasure and the 2b
sweep can find everything about a repo by prefix, without a row pointing at it.
The name is also the seal's associated data, so a sealed file renamed on disk
fails to open rather than being read as another blob's content.

Writes are atomic: a temporary file in the same directory, fsynced, renamed
over the target, and the directory fsynced, so a crash leaves the old blob or
the new one and never half of either.
"""

import os
import re
import time
from typing import List, Optional

_SEGMENT = re.compile(r"[a-z0-9._-]+")
_TMP_MARK = ".tmp-"


class BlobNameError(ValueError):
    """A name that could escape the store or is not one of ours."""


def check_name(name: str) -> str:
    segments = name.split("/")
    if not name or any(not _SEGMENT.fullmatch(s) or s in (".", "..") or _TMP_MARK in s
                       for s in segments):
        raise BlobNameError(f"not a blob name: {name!r}")
    return name


def transcript_name(repo: str, pr: int, mode: str, persona: str, now: float) -> str:
    """transcripts/<owner>/<name>/pr-<n>/<mode>/<persona>/<utc stamp>-<random>.jsonl

    Immutable: every commit writes a new name, which is what lets the commit
    point write the blob first and point the pair at it second.
    """
    owner, _, repo_name = repo.lower().partition("/")
    # Lowercase like every other segment: 19700101t000000z.
    stamp = time.strftime("%Y%m%dt%H%M%Sz", time.gmtime(now))
    return check_name(f"transcripts/{owner}/{repo_name}/pr-{int(pr)}/{mode}/{persona}/"
                      f"{stamp}-{os.urandom(4).hex()}.jsonl")


def transcripts_prefix(repo: Optional[str] = None, pr: Optional[int] = None) -> str:
    """The prefix for every transcript, one repo's, or one PR's."""
    if repo is None:
        return "transcripts"
    owner, _, repo_name = repo.lower().partition("/")
    prefix = f"transcripts/{owner}/{repo_name}"
    return check_name(prefix if pr is None else f"{prefix}/pr-{int(pr)}")


def _private_dir(path: str) -> None:
    """Create a directory if absent and make it 0700 either way.

    os.makedirs applies its mode to the leaf alone and never tightens a
    directory that already exists, and a blob's directories name its repo.
    """
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    os.chmod(path, 0o700)


class Blobs:
    def __init__(self, root: str, sealer):
        self.root = root
        self.sealer = sealer
        parent = os.path.dirname(os.path.abspath(root))
        os.makedirs(parent, exist_ok=True)
        _private_dir(root)

    def _path(self, name: str) -> str:
        return os.path.join(self.root, *check_name(name).split("/"))

    def put(self, name: str, data: bytes) -> None:
        path = self._path(name)
        directory = self.root
        for segment in check_name(name).split("/")[:-1]:
            directory = os.path.join(directory, segment)
            _private_dir(directory)
        tmp = f"{path}{_TMP_MARK}{os.urandom(4).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.sealer.seal(data, name.encode("utf-8")))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def get(self, name: str) -> Optional[bytes]:
        """The blob's plaintext, or None when there is no such blob."""
        try:
            with open(self._path(name), "rb") as f:
                sealed = f.read()
        except FileNotFoundError:
            return None
        return self.sealer.open(sealed, name.encode("utf-8"))

    def delete(self, name: str) -> bool:
        try:
            os.unlink(self._path(name))
        except FileNotFoundError:
            return False
        return True

    def list(self, prefix: str) -> List[str]:
        """Every blob name under a prefix, sorted. Temporary files are not blobs."""
        base = self._path(prefix)
        if os.path.isfile(base):
            return [prefix]
        names = []
        for dirpath, _dirs, files in os.walk(base):
            rel = os.path.relpath(dirpath, self.root)
            for f in files:
                if _TMP_MARK in f:
                    continue
                parts = [] if rel == "." else rel.split(os.sep)
                names.append("/".join(parts + [f]))
        return sorted(names)

    def delete_prefix(self, prefix: str) -> int:
        """Delete every blob under a prefix. Returns how many."""
        count = 0
        for name in self.list(prefix):
            count += self.delete(name)
        return count
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_blobs.py'`, then `./test-python.sh`
Expected: PASS, 18 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/blobs.py tests/test_hosted_blobs.py
git commit -m "hosted: the blob store, sealed and named by what it holds"
```

### Task 5: The comment scan and the signature

**Files:**
- Create: `hosted/scan.py`
- Test: `tests/test_hosted_scan.py`

**Interfaces:**
- Consumes: `signals.is_own` and `signals.MARKER` from `reviewer/signals.py` (unchanged).
- Produces:
  - `scan.DETECTORS`: a tuple of `(name, compiled pattern)`. The names are `claudebox`, `github`, `anthropic`, `openai`, `aws`, `slack`, `google` and `private_key`.
  - `scan.LITERAL_MIN = 8`, `scan.SIGNATURE` (equal to `signals.MARKER`).
  - `scan.Match(detector: str, start: int, end: int)`, frozen.
  - `scan.Scanner(literals: Mapping[str, str])` with `find(body) -> List[Match]` (sorted by start), `first(body) -> Optional[str]`, `redact(body) -> str` and the `skipped: Tuple[str, ...]` attribute (names of literals under `LITERAL_MIN`). A literal's detector name is `literal:<NAME>`.
  - `scan.sign(body: str) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_scan.py`:

```python
import _path  # noqa: F401

import unittest

import signals
from scan import LITERAL_MIN, SIGNATURE, Scanner, sign

SAMPLES = {
    "claudebox": "cbx_p_0123456789abcdef_" + "A" * 43,
    "github": "ghp_" + "a" * 36,
    "anthropic": "sk-ant-api03-" + "b" * 30,
    "openai": "sk-proj-" + "c" * 30,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack": "xoxb-1234567890-abc",
    "google": "AIza" + "d" * 35,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
}


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner({})

    def test_each_shape_is_found_under_its_own_name(self):
        for name, sample in SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(self.scanner.first(f"see `{sample}` here"), name)

    def test_every_slack_token_family_is_found(self):
        for prefix in ("xoxa", "xoxb", "xoxc", "xoxd", "xoxe", "xoxp", "xoxr", "xoxs"):
            with self.subTest(prefix=prefix):
                self.assertEqual(self.scanner.first(f"{prefix}-1234567890-abc"), "slack")

    def test_a_fine_grained_github_pat_is_found(self):
        self.assertEqual(self.scanner.first("github_pat_" + "x" * 30), "github")

    def test_an_anthropic_key_is_named_anthropic_not_openai(self):
        self.assertEqual(self.scanner.first(SAMPLES["anthropic"]), "anthropic")

    def test_ordinary_review_text_is_clean(self):
        clean = ("Commit 4f2a9c1e0b7d3a5f6e8c9d0a1b2c3d4e5f6a7b8c changes `parse_pr_ids`; "
                 "the base64 fixture `aGVsbG8gd29ybGQ=` and AKIA in prose are fine. "
                 "-----BEGIN PUBLIC KEY----- is not secret.")
        self.assertIsNone(self.scanner.first(clean))

    def test_an_aws_id_glued_inside_a_longer_token_is_not_matched(self):
        self.assertIsNone(self.scanner.first("XAKIAABCDEFGHIJKLMNOP"))


class LiteralTest(unittest.TestCase):
    def test_a_deployment_literal_is_found_by_name_and_never_echoed(self):
        scanner = Scanner({"OLLAMA_API_KEY": "olk-plain-value-123"})
        found = scanner.find("leaked olk-plain-value-123 oops")
        self.assertEqual([m.detector for m in found], ["literal:OLLAMA_API_KEY"])
        self.assertNotIn("olk-plain-value-123", repr(found))

    def test_every_occurrence_is_found(self):
        scanner = Scanner({"K": "abcdefgh"})
        self.assertEqual(len(scanner.find("abcdefgh and abcdefgh")), 2)

    def test_a_short_literal_is_skipped_and_named(self):
        self.assertEqual(LITERAL_MIN, 8)
        scanner = Scanner({"SHORT": "abc", "EMPTY": "", "LONG": "abcdefgh"})
        self.assertEqual(scanner.skipped, ("SHORT",))
        self.assertIsNone(scanner.first("abc"))


class RedactTest(unittest.TestCase):
    def test_matches_are_replaced_with_their_detector(self):
        scanner = Scanner({"K": "literal-value-1"})
        body = f"a {SAMPLES['github']} b literal-value-1 c"
        self.assertEqual(scanner.redact(body),
                         "a [withheld:github] b [withheld:literal:K] c")

    def test_overlapping_matches_redact_the_whole_span_once(self):
        key = SAMPLES["anthropic"]
        scanner = Scanner({"ANTHROPIC_API_KEY": key})
        redacted = scanner.redact(f"x {key} y")
        self.assertNotIn(key[8:], redacted)
        self.assertEqual(redacted.count("[withheld:"), 1)

    def test_a_clean_body_is_unchanged(self):
        self.assertEqual(Scanner({}).redact("nothing here"), "nothing here")


class SignTest(unittest.TestCase):
    def test_an_unsigned_body_gets_the_signature(self):
        self.assertEqual(sign("Finding.\n"), f"Finding.\n\n{SIGNATURE}")

    def test_a_persona_signed_body_is_unchanged(self):
        body = "Finding.\n\n-claudebox (Sage)"
        self.assertEqual(sign(body), body)

    def test_a_quoted_signature_does_not_count(self):
        body = "> earlier -claudebox (Sage)\n\nI disagree."
        self.assertTrue(sign(body).endswith(SIGNATURE))

    def test_whatever_sign_returns_is_own_to_the_change_gate(self):
        for body in ("x", "> -claudebox\ny", "a\n\n-ClaudeBox (SME)"):
            with self.subTest(body=body):
                self.assertTrue(signals.is_own(sign(body)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_scan.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'scan'`.

- [ ] **Step 3: Implement**

`hosted/scan.py`:

```python
"""The comment scan: credential shapes, the deployment's own literals, and the signature.

Every finding is scanned when the control plane accepts it, which is what makes
`withheld` synchronous for the pass that posted it, and again before it posts.
A match is never posted. The literal check covers the case no shape can: the
deployment's own provider keys, read from the secret store for this purpose
alone, whatever format their vendor uses.

The detectors are deliberately specific. A broad "high-entropy string" rule
would withhold commit SHAs and base64 test fixtures, which a reviewer quotes
constantly, and a scanner that withholds real findings gets turned off.
"""

import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

import signals as signals_mod

# (name, pattern). Order matters only for which name a match carries when two
# overlap at the same start: the more specific shape comes first.
DETECTORS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in (
        ("claudebox", r"cbx_[bcp]_[0-9a-f]{16}_[A-Za-z0-9_-]{43}"),
        ("github", r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        ("anthropic", r"sk-ant-[A-Za-z0-9_-]{20,}"),
        ("openai", r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        ("aws", r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
        ("slack", r"xox[a-z]-[A-Za-z0-9-]{10,}"),
        ("google", r"AIza[0-9A-Za-z_-]{35}"),
        ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ))

# A literal shorter than this is not scanned for: a four-character value would
# withhold every comment that happens to contain those four characters. No
# real provider credential is this short; a deployment whose is gets a startup
# warning from the caller rather than a scanner that cries wolf.
LITERAL_MIN = 8

# The change gate's own marker, so the two can never disagree.
SIGNATURE = signals_mod.MARKER


@dataclass(frozen=True)
class Match:
    detector: str
    start: int
    end: int


class Scanner:
    def __init__(self, literals: Mapping[str, str]):
        """`literals` maps a secret's name to its value. Names appear in results; values never do."""
        self._literals = tuple(
            (name, value) for name, value in sorted(literals.items())
            if value and len(value) >= LITERAL_MIN)
        self.skipped = tuple(sorted(name for name, value in literals.items()
                                    if value and len(value) < LITERAL_MIN))

    def find(self, body: str) -> List[Match]:
        """Every match, ordered by position. Overlapping matches are all reported."""
        found = []
        for name, pattern in DETECTORS:
            found.extend(Match(name, m.start(), m.end()) for m in pattern.finditer(body))
        for name, value in self._literals:
            start = body.find(value)
            while start != -1:
                found.append(Match(f"literal:{name}", start, start + len(value)))
                start = body.find(value, start + 1)
        return sorted(found, key=lambda m: (m.start, -m.end, m.detector))

    def first(self, body: str) -> Optional[str]:
        """The detector name of the earliest match, or None when the body is clean."""
        found = self.find(body)
        return found[0].detector if found else None

    def redact(self, body: str) -> str:
        """The body with every matched span replaced by [withheld:<detector>]."""
        out, pos = [], 0
        for m in self.find(body):
            if m.start < pos:
                # Overlaps a span already replaced; extend over its tail.
                if m.end > pos:
                    pos = m.end
                continue
            out.append(body[pos:m.start])
            out.append(f"[withheld:{m.detector}]")
            pos = m.end
        out.append(body[pos:])
        return "".join(out)


def sign(body: str) -> str:
    """The body with the -claudebox signature, unless it already carries one.

    "Already carries one" is signals.is_own's test, the same one the change gate
    uses, so a comment this adds a signature to is exactly a comment the gate
    would otherwise have read as a human's.
    """
    if signals_mod.is_own(body):
        return body
    return body.rstrip("\n") + f"\n\n{SIGNATURE}"
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_scan.py'`, then `./test-python.sh`
Expected: PASS, 16 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/scan.py tests/test_hosted_scan.py
git commit -m "hosted: the comment scan and the signature"
```

### Task 6: The GitHub App client

**Files:**
- Create: `hosted/github_app.py`
- Test: `tests/test_hosted_github_app.py`

**Interfaces:**
- Consumes: a Signer (Task 1's `Rs256Signer` shape: `sign(message) -> bytes`). The tests pass a fake one.
- Produces:
  - Constants: `API`, `API_VERSION = "2022-11-28"`, `CHECK_RUN_NAME = "claudebox"`, `READ_PERMISSIONS`, `SUMMARY_MAX = 65535`, `MAX_PAGES = 100`.
  - `github_app.Response(status: int, headers: Dict[str, str], body: bytes)`, where header names are lowercased; `Transport = Callable[[method, url, headers, body], Response]`; `urllib_transport` (follows no redirect).
  - `github_app.GitHubError(status, message, retry_after=None)` with the `retryable` property.
  - `app_jwt(app_id, signer, now) -> str`, `parse_time(stamp) -> int`, `check_repo(full_name) -> str`.
  - `App(app_id, signer, transport=urllib_transport, clock=time.time, api=API)` with `installations()`, `installation_token(installation_id, repository_ids=None, permissions=None) -> (token, expires_at)`, `read_token(installation_id, repo_id) -> (token, expires_at)` and `installation(installation_id) -> Installation`.
  - `Installation` with `repos()`, `repo(full_name) -> Optional[dict]`, `open_prs(full_name)`, `file_at_default(full_name, path) -> Optional[bytes]`, `is_ancestor(full_name, ancestor, head) -> bool` (commit ids only; a 404 is `False`), `issue_comments(full_name, pr)`, `reviews(full_name, pr)`, `newest_review_comments(full_name, pr, n=30)`, `comment(full_name, pr, body) -> int`, `create_check_run(full_name, head_sha, status, title, summary, conclusion=None) -> int` and `update_check_run(full_name, run_id, status, title, summary, conclusion=None)`.
  - `is_fork_pr(pr: Mapping) -> bool`, `user_for_token(user_token, transport=..., api=API) -> {id, login}` and `public_user(login, transport=..., api=API) -> {id, login}`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_github_app.py`:

```python
import _path  # noqa: F401

import base64
import json
import unittest

from github_app import (API, CHECK_RUN_NAME, READ_PERMISSIONS, SUMMARY_MAX, App, GitHubError,
                        Response, app_jwt, is_fork_pr, parse_time, public_user, user_for_token)


class FakeSigner:
    def __init__(self):
        self.signed = []

    def sign(self, message):
        self.signed.append(message)
        return b"signature"


class Seq(list):
    """Successive answers to one route, consumed in order."""


class FakeTransport:
    """Answers (method, path-with-query) from a table and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, json.loads(body) if body else None))
        path = url[len(API):] if url.startswith(API) else url
        answer = self.routes[(method, path)]
        if isinstance(answer, Seq):
            answer = answer.pop(0)
        if isinstance(answer, Response):
            return answer
        return Response(200, {}, json.dumps(answer).encode())


def b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


TOKEN_ANSWER = {"token": "ghs_installation", "expires_at": "2026-10-10T13:00:00Z"}
T0 = parse_time("2026-10-10T12:00:00Z")


def app_with(routes, now=T0):
    transport = FakeTransport(routes)
    clock = [now]
    app = App("12345", FakeSigner(), transport, clock=lambda: clock[0])
    return app, transport, clock


class JwtTest(unittest.TestCase):
    def test_the_jwt_is_rs256_backdated_and_names_the_app(self):
        signer = FakeSigner()
        token = app_jwt("12345", signer, 1000)
        header, payload, sig = token.split(".")
        self.assertEqual(b64json(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(b64json(payload), {"iat": 940, "exp": 1540, "iss": "12345"})
        self.assertEqual(signer.signed, [f"{header}.{payload}".encode()])
        self.assertEqual(sig, "c2lnbmF0dXJl")


class AppTest(unittest.TestCase):
    def test_an_installation_token_is_minted_with_the_jwt(self):
        app, t, _ = app_with({("POST", "/app/installations/7/access_tokens"): TOKEN_ANSWER})
        self.assertEqual(app.installation_token(7), ("ghs_installation", T0 + 3600))
        method, _url, headers, body = t.requests[0]
        self.assertTrue(headers["Authorization"].startswith("Bearer ey"))
        self.assertEqual(headers["X-GitHub-Api-Version"], "2022-11-28")
        self.assertEqual(body, {})

    def test_a_read_token_is_narrowed_to_one_repo_and_read_permissions(self):
        app, t, _ = app_with({("POST", "/app/installations/7/access_tokens"): TOKEN_ANSWER})
        app.read_token(7, 99)
        self.assertEqual(t.requests[0][3], {"repository_ids": [99], "permissions": READ_PERMISSIONS})
        self.assertEqual(READ_PERMISSIONS, {"contents": "read", "pull_requests": "read",
                                            "issues": "read", "metadata": "read"})

    def test_installations_follow_pagination(self):
        page2 = API + "/app/installations?per_page=100&page=2"
        app, t, _ = app_with({
            ("GET", "/app/installations?per_page=100"):
                Response(200, {"link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
                         json.dumps([{"id": 1}]).encode()),
            ("GET", "/app/installations?per_page=100&page=2"): [{"id": 2}],
        })
        self.assertEqual([i["id"] for i in app.installations()], [1, 2])

    def test_a_next_link_off_the_api_host_is_refused_before_any_request(self):
        app, t, _ = app_with({
            ("GET", "/app/installations?per_page=100"):
                Response(200, {"link": '<https://evil.example/steal>; rel="next"'},
                         json.dumps([{"id": 1}]).encode()),
        })
        with self.assertRaises(GitHubError) as ctx:
            app.installations()
        self.assertIn("refusing a URL off", str(ctx.exception))
        self.assertEqual(len(t.requests), 1)


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.routes = {("POST", "/app/installations/7/access_tokens"): TOKEN_ANSWER}
        self.app, self.t, self.clock = app_with(self.routes)
        self.inst = self.app.installation(7)

    def test_the_installation_token_is_reused_until_five_minutes_before_expiry(self):
        self.routes[("GET", "/repos/acme/web")] = {"id": 1}
        self.inst.repo("acme/web")
        self.clock[0] = T0 + 3600 - 301
        self.inst.repo("acme/web")
        mints = [r for r in self.t.requests if r[0] == "POST"]
        self.assertEqual(len(mints), 1)
        self.clock[0] = T0 + 3600 - 300
        self.routes[("POST", "/app/installations/7/access_tokens")] = TOKEN_ANSWER
        self.inst.repo("acme/web")
        self.assertEqual(len([r for r in self.t.requests if r[0] == "POST"]), 2)
        self.assertEqual(self.t.requests[-1][2]["Authorization"], "token ghs_installation")

    def test_a_missing_repo_is_none(self):
        self.routes[("GET", "/repos/acme/gone")] = Response(404, {}, b'{"message":"Not Found"}')
        self.assertIsNone(self.inst.repo("acme/gone"))

    def test_repos_come_from_the_repositories_key(self):
        self.routes[("GET", "/installation/repositories?per_page=100")] = {
            "total_count": 1, "repositories": [{"full_name": "acme/web"}]}
        self.assertEqual(self.inst.repos(), [{"full_name": "acme/web"}])

    def test_a_file_on_the_default_branch_is_decoded_and_no_ref_is_sent(self):
        self.routes[("GET", "/repos/acme/web/contents/.github/claudebox.json")] = {
            "type": "file", "encoding": "base64",
            "content": base64.b64encode(b'{"version": 1}').decode()}
        self.assertEqual(self.inst.file_at_default("acme/web", ".github/claudebox.json"),
                         b'{"version": 1}')
        self.assertNotIn("ref=", self.t.requests[-1][1])

    def test_an_absent_file_is_none(self):
        self.routes[("GET", "/repos/acme/web/contents/.github/claudebox.json")] = Response(
            404, {}, b'{"message":"Not Found"}')
        self.assertIsNone(self.inst.file_at_default("acme/web", ".github/claudebox.json"))

    def test_a_directory_or_an_oversized_file_is_an_error(self):
        path = ("GET", "/repos/acme/web/contents/.github/claudebox.json")
        self.routes[path] = Seq([{"type": "dir"}, {"type": "file", "encoding": "none", "size": 2_000_000}])
        with self.assertRaises(GitHubError):
            self.inst.file_at_default("acme/web", ".github/claudebox.json")
        with self.assertRaises(GitHubError) as ctx:
            self.inst.file_at_default("acme/web", ".github/claudebox.json")
        self.assertIn("2000000 bytes", str(ctx.exception))

    def test_a_repo_name_that_could_alter_the_path_is_refused(self):
        for bad in ("acme/web/../../x", "acme", "acme/web?x=1", "", "../web", "./web",
                    "acme/..", "acme/."):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.inst.repo(bad)

    def test_a_check_run_is_named_claudebox_and_its_summary_capped(self):
        self.routes[("POST", "/repos/acme/web/check-runs")] = {"id": 55}
        run_id = self.inst.create_check_run("acme/web", "abc", "queued", "Queued",
                                            "x" * (SUMMARY_MAX + 10))
        self.assertEqual(run_id, 55)
        body = self.t.requests[-1][3]
        self.assertEqual(body["name"], CHECK_RUN_NAME)
        self.assertEqual(body["head_sha"], "abc")
        self.assertEqual(len(body["output"]["summary"]), SUMMARY_MAX)
        self.assertNotIn("conclusion", body)

    def test_completing_a_check_run_sends_its_conclusion(self):
        self.routes[("PATCH", "/repos/acme/web/check-runs/55")] = {}
        self.inst.update_check_run("acme/web", 55, "completed", "Reviewed", "s", "neutral")
        self.assertEqual(self.t.requests[-1][3]["conclusion"], "neutral")

    def test_a_comment_returns_its_id(self):
        self.routes[("POST", "/repos/acme/web/issues/3/comments")] = {"id": 9}
        self.assertEqual(self.inst.comment("acme/web", 3, "hi"), 9)
        self.assertEqual(self.t.requests[-1][3], {"body": "hi"})

    def test_is_ancestor_reads_the_compare_status(self):
        old, new = "a" * 40, "b" * 40
        path = f"/repos/acme/web/compare/{old}...{new}?per_page=1"
        for status, expected in (("ahead", True), ("identical", True),
                                 ("diverged", False), ("behind", False)):
            with self.subTest(status=status):
                self.routes[("GET", path)] = {"status": status}
                self.assertEqual(self.inst.is_ancestor("acme/web", old, new), expected)

    def test_a_commit_github_no_longer_has_is_not_an_ancestor(self):
        old, new = "a" * 40, "b" * 40
        self.routes[("GET", f"/repos/acme/web/compare/{old}...{new}?per_page=1")] = Response(
            404, {}, b'{"message":"Not Found"}')
        self.assertFalse(self.inst.is_ancestor("acme/web", old, new))

    def test_is_ancestor_refuses_a_value_that_is_not_a_commit_id(self):
        for bad in ("main", "a" * 39, "../x", "A" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.inst.is_ancestor("acme/web", bad, "b" * 40)

    def test_newest_review_comments_ask_for_thirty_newest(self):
        path = "/repos/acme/web/pulls/3/comments?sort=created&direction=desc&per_page=30"
        self.routes[("GET", path)] = []
        self.assertEqual(self.inst.newest_review_comments("acme/web", 3), [])


class ErrorTest(unittest.TestCase):
    def setUp(self):
        self.routes = {("POST", "/app/installations/7/access_tokens"): TOKEN_ANSWER}
        self.app, _, _ = app_with(self.routes)
        self.inst = self.app.installation(7)

    def fail(self, response):
        self.routes[("GET", "/repos/acme/web")] = response
        with self.assertRaises(GitHubError) as ctx:
            self.inst.repo("acme/web")
        return ctx.exception

    def test_retry_after_is_read_and_retryable(self):
        e = self.fail(Response(403, {"retry-after": "30"}, b'{"message":"secondary"}'))
        self.assertEqual((e.status, e.retry_after, e.retryable), (403, 30, True))

    def test_an_exhausted_rate_limit_waits_until_its_reset(self):
        e = self.fail(Response(403, {"x-ratelimit-remaining": "0",
                                     "x-ratelimit-reset": str(T0 + 120)}, b"{}"))
        self.assertEqual(e.retry_after, 120)

    def test_a_5xx_is_retryable_and_a_422_is_not(self):
        self.assertTrue(self.fail(Response(502, {}, b"bad gateway")).retryable)
        self.assertFalse(self.fail(Response(422, {}, b'{"message":"locked"}')).retryable)

    def test_a_plain_403_is_not_retryable(self):
        e = self.fail(Response(403, {"x-ratelimit-remaining": "10"}, b'{"message":"no"}'))
        self.assertFalse(e.retryable)
        self.assertEqual(e.message, "no")


class RedirectTest(unittest.TestCase):
    """urllib_transport against real loopback servers: a 30x is never followed."""

    def serve(self, handler_cls):
        import http.server
        import threading
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_an_authenticated_redirect_off_host_is_not_followed(self):
        import http.server
        from github_app import urllib_transport
        seen = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        other = self.serve(Elsewhere)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_port}/steal")
                self.end_headers()

            def log_message(self, *a):
                pass

        first = self.serve(Redirector)
        resp = urllib_transport("GET", f"http://127.0.0.1:{first.server_port}/x",
                                {"Authorization": "token secret"}, None)
        self.assertEqual(resp.status, 302)
        self.assertEqual(seen, [])


class ForkTest(unittest.TestCase):
    def pr(self, head, base="acme/web"):
        return {"head": {"repo": None if head is None else {"full_name": head}},
                "base": {"repo": {"full_name": base}}}

    def test_a_branch_in_the_repo_itself_is_not_a_fork(self):
        self.assertFalse(is_fork_pr(self.pr("acme/web")))

    def test_case_does_not_matter(self):
        self.assertFalse(is_fork_pr(self.pr("Acme/Web")))

    def test_another_repo_is_a_fork(self):
        self.assertTrue(is_fork_pr(self.pr("someone/web")))

    def test_a_deleted_head_repo_is_treated_as_a_fork(self):
        self.assertTrue(is_fork_pr(self.pr(None)))
        self.assertTrue(is_fork_pr({}))


class UserTest(unittest.TestCase):
    def test_user_for_token_resolves_the_numeric_id(self):
        t = FakeTransport({("GET", "/user"): {"id": 42, "login": "octo"}})
        self.assertEqual(user_for_token("gho_x", t), {"id": 42, "login": "octo"})
        self.assertEqual(t.requests[0][2]["Authorization"], "Bearer gho_x")

    def test_public_user_sends_no_credential(self):
        t = FakeTransport({("GET", "/users/octo"): {"id": 42, "login": "octo"}})
        self.assertEqual(public_user("octo", t)["id"], 42)
        self.assertNotIn("Authorization", t.requests[0][2])

    def test_public_user_refuses_a_login_that_could_alter_the_path(self):
        for bad in ("octo/../x", "-octo", "", "a" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                public_user(bad, FakeTransport({}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_github_app.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'github_app'`.

- [ ] **Step 3: Implement**

`hosted/github_app.py`:

```python
"""GitHub, as the App: its JWT, installation tokens, and the REST calls 2a makes.

Every request goes through a transport, a function from (method, url, headers,
body) to a Response, so the tests stub GitHub at that one seam the way
test-python.sh stubs subprocess. urllib_transport is the real one.

Two kinds of token. The App's JWT, signed with its private key, mints
installation tokens and does nothing else. An Installation client mints itself
a full installation token and refreshes it five minutes before expiry, for the
control plane's own reads and writes. A worker's read token is minted
separately, narrowed by GitHub to one repository and read permissions
(READ_PERMISSIONS), so GitHub enforces the repo boundary, not claudebox.
"""

import base64
import calendar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "claudebox-hosted"
CHECK_RUN_NAME = "claudebox"
# The spec's "narrowed at mint time to contents, pull requests, issues and
# metadata, read only".
READ_PERMISSIONS = {"contents": "read", "pull_requests": "read",
                    "issues": "read", "metadata": "read"}
# A check run's summary is capped by GitHub at 65535 characters.
SUMMARY_MAX = 65535
# A runaway Link chain stops here rather than paging forever.
MAX_PAGES = 100

_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
# A commit id: SHA-1 (40 hex) or SHA-256 (64 hex).
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class Response:
    status: int
    headers: Dict[str, str]  # lowercased names
    body: bytes


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Response]


class GitHubError(Exception):
    def __init__(self, status: int, message: str, retry_after: Optional[int] = None):
        super().__init__(f"GitHub {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """Only Retry-After and 5xx are worth retrying; status 0 is unreachable."""
        return self.retry_after is not None or self.status >= 500 or self.status == 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. urllib would replay the Authorization header to
    wherever a 30x pointed, off the API host included, so a redirect comes
    back as the 30x it is and the caller treats it as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: Dict[str, str],
                     body: Optional[bytes]) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
    except (urllib.error.URLError, OSError) as e:
        raise GitHubError(0, f"cannot reach GitHub: {getattr(e, 'reason', e)}") from None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def app_jwt(app_id: str, signer, now: float) -> str:
    """The App's JWT. Backdated a minute for clock skew; GitHub allows ten minutes."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(app_id)},
        separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_b64url(signer.sign(signing_input))}"


def parse_time(stamp: str) -> int:
    """GitHub's `2026-10-10T12:00:00Z` as Unix seconds."""
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _retry_after(resp: Response, now: float) -> Optional[int]:
    value = resp.headers.get("retry-after")
    if value is not None and value.strip().isdigit():
        return max(1, int(value))
    if resp.status in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "")
        return max(1, int(reset) - int(now)) if reset.isdigit() else 60
    return None


def _next_link(headers: Mapping[str, str]) -> Optional[str]:
    for part in headers.get("link", "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def check_repo(full_name: str) -> str:
    """An owner/name safe to put in a URL path: no `.` or `..` segment, nothing else."""
    if not _REPO_RE.fullmatch(full_name or "") or any(
            part in (".", "..") for part in full_name.split("/")):
        raise ValueError(f"not a repository name: {full_name!r}")
    return full_name


class _Client:
    def __init__(self, transport: Transport, clock: Callable[[], float], api: str):
        self.transport = transport
        self.clock = clock
        self.api = api

    def _auth(self) -> str:
        raise NotImplementedError

    def _send(self, method: str, url: str, payload: Any = None) -> Response:
        if url.startswith("/"):
            url = self.api + url
        elif not url.startswith(self.api + "/"):
            # A Link header is GitHub's text; a next page off the API host would
            # carry this client's token somewhere else.
            raise GitHubError(0, f"refusing a URL off {self.api}: {url}")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT}
        auth = self._auth()
        if auth:
            headers["Authorization"] = auth
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self.transport(method, url, headers, body)

    def _call(self, method: str, url: str, payload: Any = None) -> Any:
        resp = self._send(method, url, payload)
        return self._parse(resp)

    def _parse(self, resp: Response) -> Any:
        if 200 <= resp.status < 300:
            return json.loads(resp.body) if resp.body else None
        try:
            message = json.loads(resp.body).get("message", "")
        except (ValueError, AttributeError):
            message = resp.body[:200].decode("utf-8", "replace")
        raise GitHubError(resp.status, message or "no message", _retry_after(resp, self.clock()))

    def _pages(self, url: str, key: Optional[str] = None) -> List[Any]:
        items: List[Any] = []
        for _ in range(MAX_PAGES):
            resp = self._send("GET", url)
            data = self._parse(resp)
            items.extend(data[key] if key else data)
            url = _next_link(resp.headers)
            if url is None:
                return items
        raise GitHubError(0, f"more than {MAX_PAGES} pages; refusing to keep paging")


class App(_Client):
    def __init__(self, app_id: str, signer, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time, api: str = API):
        super().__init__(transport, clock, api)
        self.app_id = str(app_id)
        self.signer = signer

    def _auth(self) -> str:
        return "Bearer " + app_jwt(self.app_id, self.signer, self.clock())

    def installations(self) -> List[dict]:
        return self._pages("/app/installations?per_page=100")

    def installation_token(self, installation_id: int,
                           repository_ids: Optional[List[int]] = None,
                           permissions: Optional[Mapping[str, str]] = None) -> Tuple[str, int]:
        """(token, expires_at). Narrowed when repository_ids or permissions are given."""
        payload: Dict[str, Any] = {}
        if repository_ids is not None:
            payload["repository_ids"] = [int(i) for i in repository_ids]
        if permissions is not None:
            payload["permissions"] = dict(permissions)
        data = self._call("POST", f"/app/installations/{int(installation_id)}/access_tokens",
                          payload)
        return data["token"], parse_time(data["expires_at"])

    def read_token(self, installation_id: int, repo_id: int) -> Tuple[str, int]:
        """A worker's token: one repository, read permissions, an hour."""
        return self.installation_token(installation_id, [repo_id], READ_PERMISSIONS)

    def installation(self, installation_id: int) -> "Installation":
        return Installation(self, installation_id)


class Installation(_Client):
    REFRESH_MARGIN = 300

    def __init__(self, app: App, installation_id: int):
        super().__init__(app.transport, app.clock, app.api)
        self.app = app
        self.installation_id = int(installation_id)
        self._token: Optional[str] = None
        self._expires_at = 0

    def _auth(self) -> str:
        if self._token is None or self._expires_at - self.clock() <= self.REFRESH_MARGIN:
            self._token, self._expires_at = self.app.installation_token(self.installation_id)
        return "token " + self._token

    def repos(self) -> List[dict]:
        return self._pages("/installation/repositories?per_page=100", key="repositories")

    def repo(self, full_name: str) -> Optional[dict]:
        """The repository, or None when it does not exist or the App cannot see it."""
        try:
            return self._call("GET", f"/repos/{check_repo(full_name)}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def open_prs(self, full_name: str) -> List[dict]:
        return self._pages(f"/repos/{check_repo(full_name)}/pulls?state=open&per_page=100")

    def file_at_default(self, full_name: str, path: str) -> Optional[bytes]:
        """A file's bytes on the default branch, or None when it is absent.

        No ref is passed, which is the API's "default branch": the spec's "read
        from the default branch only, so a PR cannot choose who reviews it".
        """
        url = f"/repos/{check_repo(full_name)}/contents/{urllib.parse.quote(path, safe='/')}"
        try:
            data = self._call("GET", url)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubError(422, f"{path} is not a file")
        if data.get("encoding") != "base64":
            raise GitHubError(413, f"{path} is {data.get('size')} bytes, too large to read")
        return base64.b64decode(data.get("content", ""))

    def is_ancestor(self, full_name: str, ancestor: str, head: str) -> bool:
        """Whether `ancestor` is reachable from `head`.

        The spec's rewrite test: a pair's last reviewed head that is no longer an
        ancestor of the current head means a rebase or force-push rewrote the
        branch, and the pass widens to the whole PR. The compare API's status is
        `ahead` or `identical` exactly when that holds. A 404 (a commit GitHub no
        longer has) is not an ancestor, so a rewrite that also dropped the old
        head still widens.
        """
        for sha in (ancestor, head):
            if not _SHA_RE.fullmatch(sha or ""):
                raise ValueError(f"not a commit id: {sha!r}")
        url = f"/repos/{check_repo(full_name)}/compare/{ancestor}...{head}?per_page=1"
        try:
            data = self._call("GET", url)
        except GitHubError as e:
            if e.status == 404:
                return False
            raise
        return data.get("status") in ("ahead", "identical")

    def issue_comments(self, full_name: str, pr: int) -> List[dict]:
        return self._pages(f"/repos/{check_repo(full_name)}/issues/{int(pr)}/comments?per_page=100")

    def reviews(self, full_name: str, pr: int) -> List[dict]:
        return self._pages(f"/repos/{check_repo(full_name)}/pulls/{int(pr)}/reviews?per_page=100")

    def newest_review_comments(self, full_name: str, pr: int, n: int = 30) -> List[dict]:
        """One page, newest first: the same cap of 30 local mode's pr_signal uses."""
        return self._call("GET", f"/repos/{check_repo(full_name)}/pulls/{int(pr)}/comments"
                                 f"?sort=created&direction=desc&per_page={int(n)}")

    def comment(self, full_name: str, pr: int, body: str) -> int:
        data = self._call("POST", f"/repos/{check_repo(full_name)}/issues/{int(pr)}/comments",
                          {"body": body})
        return data["id"]

    def create_check_run(self, full_name: str, head_sha: str, status: str, title: str,
                         summary: str, conclusion: Optional[str] = None) -> int:
        payload = {"name": CHECK_RUN_NAME, "head_sha": head_sha, "status": status,
                   "output": {"title": title, "summary": summary[:SUMMARY_MAX]}}
        if conclusion is not None:
            payload["conclusion"] = conclusion
        return self._call("POST", f"/repos/{check_repo(full_name)}/check-runs", payload)["id"]

    def update_check_run(self, full_name: str, run_id: int, status: str, title: str,
                         summary: str, conclusion: Optional[str] = None) -> None:
        payload = {"status": status, "output": {"title": title, "summary": summary[:SUMMARY_MAX]}}
        if conclusion is not None:
            payload["conclusion"] = conclusion
        self._call("PATCH", f"/repos/{check_repo(full_name)}/check-runs/{int(run_id)}", payload)


def is_fork_pr(pr: Mapping[str, Any]) -> bool:
    """The spec's fork rule: the head repository alone decides.

    A head repository GitHub reports as null (a deleted fork) is a fork: the
    rule fails closed, and an unknown head is never reviewed.
    """
    head_repo = (pr.get("head") or {}).get("repo")
    base_repo = (pr.get("base") or {}).get("repo") or {}
    if not head_repo:
        return True
    return (head_repo.get("full_name") or "").lower() != (base_repo.get("full_name") or "").lower()


class _Bare(_Client):
    """A client with a fixed Authorization value; empty means anonymous."""

    def __init__(self, auth: str, transport: Transport, api: str):
        super().__init__(transport, time.time, api)
        self._header = auth

    def _auth(self) -> str:
        return self._header


def user_for_token(user_token: str, transport: Transport = urllib_transport,
                   api: str = API) -> dict:
    """{id, login} of the user a user token belongs to. What admin auth resolves."""
    data = _Bare("Bearer " + user_token, transport, api)._call("GET", "/user")
    return {"id": int(data["id"]), "login": data["login"]}


def public_user(login: str, transport: Transport = urllib_transport, api: str = API) -> dict:
    """{id, login} from GitHub's public user API, for `hosted whois`. No credential."""
    if not _LOGIN_RE.fullmatch(login or ""):
        raise ValueError(f"not a GitHub login: {login!r}")
    data = _Bare("", transport, api)._call("GET", f"/users/{login}")
    return {"id": int(data["id"]), "login": data["login"]}
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_github_app.py'`, then `./test-python.sh`
Expected: PASS, 31 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/github_app.py tests/test_hosted_github_app.py
git commit -m "hosted: the GitHub App client, over one transport seam"
```

### Task 7: Documentation

**Files:**
- Modify: `CLAUDE.md` (the file inventory in "What this is", a new "Hosted App libraries" subsection at the end of "Architecture", and one bullet under "Gotchas when editing")
- Modify: `HISTORY.md` (one entry under "Unreleased")

**Interfaces:**
- Consumes: the module names and behavior from Tasks 1–6.
- Produces: nothing code depends on.

- [ ] **Step 1: Add `hosted/` to the inventory**

In `CLAUDE.md`'s "What this is", the sentence that lists the repository's files, insert this after the `reviewer/` clause:

```markdown
`hosted/` (the hosted App's control-plane libraries, built up through phase 2a; local mode imports none of it),
```

- [ ] **Step 2: Add the subsection**

At the end of "Architecture", before "## Configuration", add:

```markdown
### Hosted App libraries

`hosted/` is the hosted GitHub App's code (`docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`), built in the sub-plans listed in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. Like `reviewer/`, its modules are flat and imported by plain name, and they import the decision core from `reviewer/` the same way. Local mode never imports anything from it.

Phase 2a.1 adds six libraries, none of which runs a process. `crypto.py` seals blobs (AES-256-GCM, with the blob's name as associated data) and signs the App JWT (RS256). `records.py` is the SQLite records store, schema version 1 with `secure_delete` on, plus `InstanceLock`, the `flock` that keeps a second control plane from serving. `creds.py` mints bootstrap tokens, capabilities and provider tokens (`cbx_<k>_<id>_<secret>`) and stores only a salted hash of each. A bootstrap token is single-use, and `revoke_all` is what a starting instance runs before it serves anything. `blobs.py` is the encrypted blob directory, with self-describing lowercase names so erasure can find a repo's blobs by prefix. `scan.py` is the comment scan (credential shapes plus the deployment's own literals) and the `-claudebox` signature. `github_app.py` is the App's GitHub client over a single transport seam: the JWT, full and narrowed installation tokens, the fork rule, and the REST calls 2a makes.

`crypto.py` is the one exception to standard-library only. Python's standard library has neither RS256 nor an AEAD cipher, so it imports Debian's `python3-cryptography`, which exists only in the control-plane image, and it imports it inside its classes so the host can still import the module. Everything else takes a Sealer or a Signer as an argument and is tested with a fake.
```

- [ ] **Step 3: Add the gotcha**

Under "Gotchas when editing", add:

```markdown
- `hosted/crypto.py` is the only module allowed to import `cryptography`, and its `SealerTest` and `SignerTest` skip on a host without it, which is every host `test-python.sh` normally runs on. A skip there is not a pass: run them under `cryptography==38.0.4` (bookworm's `python3-cryptography`) after touching that file. From phase 2a.4, `hosted/Dockerfile` runs them at build time.
```

- [ ] **Step 4: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.1 of the hosted GitHub App: the libraries the control plane is built from.** Six modules under `hosted/`, none of which runs anything yet. A SQLite records store (schema version 1, `secure_delete` on) and the lock file that keeps a second control plane from serving. Credentials stored as salted hashes, never as tokens: single-use bootstrap tokens, job capabilities, per-pair provider tokens, and a revoke-everything for a starting instance. An encrypted blob directory whose names say what each blob holds. The comment scan for credential shapes and the deployment's own secrets, and the `-claudebox` signature. A GitHub App client that signs its JWT, mints installation tokens narrowed to one repo and read permissions, and refuses to follow a pagination link off GitHub's API host. Blob encryption and JWT signing need a library the standard library lacks, so they come from Debian's `python3-cryptography` in the control-plane image, behind one module. Local mode is unchanged.
```

- [ ] **Step 5: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && ./test-launcher.sh`
Expected: all five pass. The Python suite reports 114 more tests than at the base, 12 of them skipped on a host without `cryptography`.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md HISTORY.md
git commit -m "docs: hosted phase 2a.1 libraries"
```
