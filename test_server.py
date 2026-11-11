import json
import unittest

import server


def usage(inp=100, cached=40, out=10):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "total_tokens": inp + out}


def event(kind, payload):
    return json.dumps({"timestamp": "2026-11-11T06:00:00Z",
                       "type": kind, "payload": payload}).encode()


def count(total, last=None):
    return event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": usage() if last is None else last},
        "rate_limits": {"primary": {"used_percent": 58}}})


def response(rid, value=None):
    return event("token_usage_record", {"response_id": rid,
                 "usage": usage() if value is None else value})


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        server.codex_files.clear()
        server.records.clear()
        server.state["version"] = 0
        server.state["codex_limit"] = None

    def feed(self, *lines):
        server.codex_lines("session.jsonl", lines)

    def test_legacy_reset_and_repeated_notifications(self):
        self.feed(count(1000), count(1000), count(110), count(110), count(220))
        self.assertEqual(len(server.records), 3)
        self.assertEqual(sum(map(server.billable, server.records)), 210)

    def test_modern_records_across_scan_boundaries_and_duplicate_response(self):
        self.feed(event("turn_context", {"model": "test-model", "cwd": "/repo/project"}),
                  response("r1"))
        self.feed(count(110), response("r1"), response("r2"), count(220))
        self.assertEqual(len(server.records), 2)
        self.assertEqual(server.records[0][1:3], ["test-model", "project"])
        self.assertEqual(server.records[0][3:7], [60, 10, 0, 40])
        self.assertEqual(server.state["codex_limit"]["used"], 58)

    def test_compaction_zero_snapshot_keeps_actual_response_usage(self):
        self.feed(response("r1"), count(110), response("compact", usage(200, 50, 30)),
                  count(110, usage(0, 0, 0)), response("r2"), count(110))
        self.assertEqual(len(server.records), 3)
        self.assertEqual(sum(map(server.billable, server.records)), 320)

    def test_legacy_to_modern_transition_and_independent_files(self):
        self.feed(count(110), response("r2"), count(220))
        server.codex_lines("other.jsonl", [count(110)])
        self.assertEqual(len(server.records), 3)


if __name__ == "__main__":
    unittest.main()
