"""End-to-end check of the real spawn path.

Not part of the unit suite: this spawns an actual detached monitor process,
exercises the real flock handoff across fork+exec, and drives it against a
local GraphQL stub. Run inside the claub image:

    python tests/integration_e2e.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor_control  # noqa: E402
from session_store import acquire_lock, find_monitor, read_manifest  # noqa: E402

PORT = 8123
# A pre-existing submission, so the baseline filter is exercised for real:
# only the one added *after* start should show up on the timeline.
OLD_SUBMISSION = {
    "id": "1", "statusDisplay": "Accepted", "lang": "python3", "langName": "Python3",
    "runtime": "50 ms", "timestamp": "1000", "memory": "17 MB",
}
STATE = {"code": "def solve():\n    pass\n", "ts": 1000, "submissions": [OLD_SUBMISSION]}
HITS = {"synced": 0, "submissions": 0}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        query = body["query"]
        if "getQuestionDetail" in query:
            payload = {"data": {"question": {"questionId": "146"}}}
        elif "syncedCode" in query:
            HITS["synced"] += 1
            payload = {"data": {"syncedCode": {"timestamp": STATE["ts"], "code": STATE["code"]}}}
        elif "submissionList" in query:
            HITS["submissions"] += 1
            payload = {"data": {"questionSubmissionList": {
                "hasNext": False, "lastKey": None, "submissions": STATE["submissions"],
            }}}
        else:
            payload = {"data": {}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" -- {detail}" if detail else ""))
    return bool(condition)


def main() -> int:
    server = HTTPServer(("127.0.0.1", PORT), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    workdir = Path(tempfile.mkdtemp(prefix="lc-e2e-"))
    root = workdir / "leetcode-sessions"
    lock_path = workdir / "monitor.lock"
    token = workdir / "token.json"
    token.write_text(json.dumps({"session": "s" * 120, "csrf": "c" * 32}))

    os.environ["LEETCODE_API_URL"] = f"http://127.0.0.1:{PORT}/graphql"
    os.environ["LEETCODE_TOKEN_FILE"] = str(token)

    ok = True
    try:
        print("\n[1] start_monitoring spawns a real detached monitor")
        out = asyncio.run(monitor_control.start(
            "lru-cache", "python3", root=root, lock_path=lock_path,
        ))
        print(f"      {out.splitlines()[0]}")
        time.sleep(3)

        found = find_monitor()
        ok &= check("monitor is discoverable by its argv[0] tag", found is not None,
                    f"pid={found['pid']}" if found else "not found")
        ok &= check("tag carries the problem slug",
                    found and found.get("problem") == "lru-cache")
        ok &= check("monitor is its own process group leader",
                    found and os.getpgid(found["pid"]) == found["pid"])

        print("\n[2] the lock survived the fork+exec handoff")
        fd = acquire_lock(lock_path)
        ok &= check("lock is held by the child, not the parent", fd is None)
        if fd is not None:
            os.close(fd)

        print("\n[3] concurrent start is refused")
        again = asyncio.run(monitor_control.start(
            "two-sum", "python3", root=root, lock_path=lock_path,
        ))
        ok &= check("second start refused, naming the live problem",
                    "lru-cache" in again, again.splitlines()[0])

        print("\n[4] an edit is recorded")
        session_dir = next(root.iterdir())
        STATE["code"] = "def solve():\n    return 42\n"
        STATE["ts"] = 2000
        STATE["submissions"] = [OLD_SUBMISSION, {
            "id": "9001", "statusDisplay": "Wrong Answer", "lang": "python3",
            "langName": "Python3", "runtime": "N/A", "timestamp": "5000",
            "memory": "N/A",
        }]
        # Submissions poll on a 30s timer, so this has to outwait one cycle.
        time.sleep(36)

        events = [json.loads(x) for x in
                  (session_dir / "events.jsonl").read_text().splitlines() if x.strip()]
        kinds = [e["type"] for e in events]
        changes = [e for e in events if e["type"] == "code_change"]
        ok &= check("monitor_start event written", "monitor_start" in kinds)
        ok &= check("code_change recorded for the edit", len(changes) >= 1,
                    f"{len(changes)} change(s)")
        if changes:
            ok &= check("diff counts are real", changes[-1]["lines_added"] >= 1,
                        f"+{changes[-1]['lines_added']}/-{changes[-1]['lines_removed']}")
            snap = session_dir / changes[-1]["snapshot_ref"]
            ok &= check("snapshot file written with the code", snap.exists()
                        and "return 42" in snap.read_text())
        subs = [e for e in events if e["type"] == "submission"]
        ok &= check("only the post-baseline submission recorded", len(subs) == 1,
                    f"{[s['status'] for s in subs]}")
        ok &= check("and it is the Wrong Answer",
                    bool(subs) and subs[0]["status"] == "Wrong Answer")

        print("\n[5] results readable while still running")
        live = monitor_control.results(root=root)
        ok &= check("live session reports as still running", "still running" in live)

        print("\n[6] stop_monitoring terminates it and finalizes")
        stopped = monitor_control.stop(root=root)
        ok &= check("stop reports the problem", "lru-cache" in stopped)
        time.sleep(1)
        ok &= check("process is gone", find_monitor() is None)
        fd = acquire_lock(lock_path)
        ok &= check("lock released after stop", fd is not None)
        if fd is not None:
            os.close(fd)
        manifest = read_manifest(session_dir)
        ok &= check("manifest finalized", manifest["stop_reason"] == "stopped"
                    and manifest["ended_at"] is not None,
                    f"stop_reason={manifest['stop_reason']}")

        print("\n[7] a fresh start works after the previous one ended")
        out2 = asyncio.run(monitor_control.start(
            "two-sum", "python3", root=root, lock_path=lock_path,
        ))
        ok &= check("second session starts cleanly", "Monitoring 'two-sum'" in out2,
                    out2.splitlines()[0])
        time.sleep(2)
        monitor_control.stop(root=root)

        print(f"\n  stub hits: syncedCode={HITS['synced']}, submissions={HITS['submissions']}")
    finally:
        leftover = find_monitor()
        if leftover:
            os.killpg(leftover["pid"], 9)
        shutil.rmtree(workdir, ignore_errors=True)
        server.shutdown()

    print(f"\n{'ALL CHECKS PASSED' if ok else 'FAILURES PRESENT'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
