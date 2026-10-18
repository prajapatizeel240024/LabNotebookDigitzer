"""The small program that runs inside a sandbox container.

It is handed to the container as `python3 -I -c <RUNNER_SOURCE> <b64 code> <b64 rows>`, so the
sandbox image needs nothing but Python. It loads the untrusted code, calls check(rows), and
prints one result line that the manager looks for. Anything else the code prints is captured
as plain output and size-capped by the manager.

This is not a security boundary. The container is. A hostile check can lie about its own
result; what it must not be able to do is touch the host, which is what the container
flags and tests/test_sandbox_escape.py are about.
"""

RESULT_MARKER = "@@LND_RESULT@@"

RUNNER_SOURCE = r"""
import base64, json, sys

MARK = "@@LND_RESULT@@"

def emit(obj):
    sys.stdout.write("\n" + MARK + json.dumps(obj) + "\n")
    sys.stdout.flush()

try:
    code = base64.b64decode(sys.argv[1]).decode()
    rows = json.loads(base64.b64decode(sys.argv[2]).decode())
    ns = {"__name__": "check"}
    exec(compile(code, "check.py", "exec"), ns)
    fn = ns.get("check")
    if not callable(fn):
        raise RuntimeError("the code does not define a function named check")
    issues = fn(rows)
    if not isinstance(issues, list) or not all(isinstance(i, str) for i in issues):
        raise TypeError("check(rows) must return a list of strings")
    emit({"ok": True, "issues": issues[:100]})
except BaseException as exc:
    emit({"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:500]})
"""
