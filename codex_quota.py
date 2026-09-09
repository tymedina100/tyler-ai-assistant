"""Read account quota through documented Codex app-server stdio RPC; no model turn."""
import json
import math
import os
import selectors
import subprocess
import time
from pathlib import Path

from codex_subscription import QuotaEvidence, SubscriptionUnavailable


def quota_from_response(result, *, bucket="codex", now=None):
    now = time.time() if now is None else now
    by_id = result.get("rateLimitsByLimitId")
    record = by_id.get(bucket) if isinstance(by_id, dict) else result.get("rateLimits")
    if not isinstance(record, dict) or record.get("limitId") != bucket:
        raise SubscriptionUnavailable("The configured quota bucket is unavailable.")
    windows = [record.get(key) for key in ("primary", "secondary") if record.get(key) is not None]
    if not windows:
        raise SubscriptionUnavailable("Quota windows are unavailable.")
    used = []
    for window in windows:
        if not isinstance(window, dict):
            raise SubscriptionUnavailable("Invalid quota window.")
        percent, reset = window.get("usedPercent"), window.get("resetsAt")
        if (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100
                or type(reset) not in (int, float) or not math.isfinite(reset) or reset <= now):
            raise SubscriptionUnavailable("Quota window is invalid or awaiting reset refresh.")
        used.append(percent)
    exhausted = bool(record.get("rateLimitReachedType")) or record.get("spendControlReached") is True
    return QuotaEvidence(now, max(used), exhausted)


def read_quota(binary, *, bucket="codex", timeout=15):
    if not Path(binary).is_absolute() or not 1 <= timeout <= 30:
        raise ValueError("Use an absolute CLI path and a bounded timeout.")
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "CODEX_HOME", "LANG") if key in os.environ}
    process = subprocess.Popen([binary, "app-server", "--stdio", "--config", 'forced_login_method="chatgpt"',
                                "--disable", "apps", "--disable", "plugins"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    total = 0
    def send(message):
        process.stdin.write((json.dumps(message)+"\n").encode())
        process.stdin.flush()
    def receive(request_id):
        nonlocal total
        while True:
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n"); buffer[:] = rest
                message = json.loads(line)
                if message.get("id") == request_id:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise SubscriptionUnavailable("Codex quota request failed.")
                    return message["result"]
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SubscriptionUnavailable("Codex quota request timed out.")
            chunk = os.read(process.stdout.fileno(), 65536)
            total += len(chunk)
            if not chunk or total > 2000000:
                raise SubscriptionUnavailable("Codex quota transport ended or exceeded its limit.")
            buffer.extend(chunk)
    try:
        send({"id":0,"method":"initialize","params":{"clientInfo":{"name":"tyleros_quota","version":"1.0"}}})
        receive(0)
        send({"method":"initialized"})
        send({"id":1,"method":"account/rateLimits/read"})
        return quota_from_response(receive(1), bucket=bucket)
    finally:
        selector.close()
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        process.stdin.close(); process.stdout.close()
