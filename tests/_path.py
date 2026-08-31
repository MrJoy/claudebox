"""Puts reviewer/ on sys.path, and guards the checkout against the test suite.

Every test module imports this, which is the only reason the guard lives in a
path shim: it is the one place the whole suite passes through, so it covers a
module that does not exist yet. The tearDownModule this replaces was scoped to
one file, and `unittest discover` runs modules alphabetically -- a damaging test
in a module sorting after test_review_loop walked straight past it and the run
still printed OK.
"""

import atexit
import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reviewer"))

_CHECKOUT_GIT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".git"
)


def _check_checkout_git_dir():
    """No test may leave this checkout's .git read-only.

    review_loop.main chmods its WORK_REPO, so a test that points it at "." locks
    the repo the suite is running from and leaves it that way. Use the
    scratch_repo helper in test_review_loop.py instead of landing here.

    .git, its immediate children and .git/refs, not a full walk: an accidentally
    recursive lock reaches .git/objects, and checking that one entry is O(1)
    where walking the object store under it is the cost the whole design avoids.
    The repair is the recursive one, because by then something is already broken.
    """
    if not os.path.isdir(_CHECKOUT_GIT):
        return
    shallow = [_CHECKOUT_GIT] + [
        os.path.join(_CHECKOUT_GIT, name) for name in os.listdir(_CHECKOUT_GIT)
        if os.path.isdir(os.path.join(_CHECKOUT_GIT, name))
    ]
    if all(os.access(path, os.W_OK) for path in shallow):
        return

    for root, dirs, _files in os.walk(_CHECKOUT_GIT):
        for name in [root] + [os.path.join(root, d) for d in dirs]:
            os.chmod(name, os.stat(name).st_mode | stat.S_IWUSR)

    sys.stdout.flush()
    sys.stderr.write(
        f"\nFATAL: a test left {_CHECKOUT_GIT} read-only (repaired); "
        "point its WORK_REPO at scratch_repo(self)\n"
    )
    sys.stderr.flush()
    # os._exit, not a raise: atexit cannot change the exit status, so a raise
    # here prints a traceback and the run still reports rc 0 -- a green that
    # left the checkout damaged, which is the exact failure this guard exists
    # to stop.
    os._exit(1)


atexit.register(_check_checkout_git_dir)
