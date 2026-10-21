import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components/bowlabs_status")
)
from protocol import POLICY, Link, Refused, decode


def report(sequence=1):
    return {
        "protocol": 1,
        "source": "synthetic",
        "sequence": sequence,
        "reportedAt": 1000,
        "modules": {
            "photos": {"state": "checked", "permission": "full"},
            "calendar": {"state": "checked", "permission": "full"},
            "music": {"state": "permission_only", "permission": "granted"},
            "album": {
                "state": "sample_loaded",
                "permission": "full",
                "loaded": 3,
                "needs_download": 0,
                "failed": 0,
                "timed_out": 0,
            },
        },
    }


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.events = []
        self.link = Link(
            lambda *event: self.events.append(event),
            clock=lambda: 1000,
            monotonic=lambda: self.now,
        )

    def paired(self):
        started = self.link.start(POLICY)
        self.link.approve(started["code"])
        self.link.confirm(started["secret"])
        return started["secret"]

    def test_both_approvals_and_single_use(self):
        value = self.link.start(POLICY)
        key = value["secret"]
        with self.assertRaises(Refused):
            self.link.confirm(key)
        with self.assertRaises(Refused):
            self.link.report(key, report())
        self.link.approve(value["code"])
        with self.assertRaises(Refused):
            self.link.report(key, report())
        self.link.confirm(key)
        with self.assertRaises(Refused):
            self.link.confirm(key)
        self.assertTrue(self.link.report(key, report())["accepted"])

    def test_tokens_hashed_only_and_scope_fixed(self):
        value = self.link.start(POLICY)
        self.assertNotIn(value["secret"], str(self.link.pending))
        self.assertEqual(value["capabilities"], ["status.report.synthetic"])
        for body in [
            {**POLICY, "role": "tv"},
            {**POLICY, "capabilities": ["health.read"]},
            {**POLICY, "protocol": True},
            {**POLICY, "extra": "private"},
        ]:
            with self.assertRaises(Refused):
                self.link.start(body)

    def test_expiry_and_clock_boundaries(self):
        key = self.paired()
        self.now = 600
        self.link.tick()
        with self.assertRaises(Refused):
            self.link.report(key, report())
        self.assertEqual(self.events[-1], ("expired", None))
        started = self.link.start(POLICY)
        self.now = 720
        with self.assertRaises(Refused):
            self.link.approve(started["code"])

    def test_replay_rejected_and_revocation_clears_state(self):
        key = self.paired()
        self.link.report(key, report(2))
        for sequence in [1, 2]:
            with self.assertRaises(Refused):
                self.link.report(key, report(sequence))
        self.link.revoke()
        with self.assertRaises(Refused):
            self.link.report(key, report(3))
        self.assertEqual(self.events[-1], ("revoked", None))

    def test_disconnect_and_recovery(self):
        key = self.paired()
        self.link.report(key, report())
        self.now = 91
        self.link.tick()
        self.assertEqual(self.events[-1], ("disconnected", None))
        self.link.report(key, report(2))
        self.assertEqual(self.events[-1][0], "connected")

    def test_no_state_mutation_for_invalid_reports(self):
        key = self.paired()
        before = list(self.events)
        cases = []
        for field, value in [
            ("source", "real"),
            ("sequence", True),
            ("reportedAt", True),
            ("reportedAt", 1031),
            ("reportedAt", 969),
            ("protocol", True),
        ]:
            cases.append({**report(), field: value})
        cases.append({**report(), "album_title": "private"})
        bad = report()
        bad["modules"]["photos"]["name"] = "private"
        cases.append(bad)
        bad = report()
        bad["modules"]["music"]["state"] = "checked"
        cases.append(bad)
        for count in [-1, 4, True, 3.0]:
            bad = report()
            bad["modules"]["album"]["loaded"] = count
            cases.append(bad)
        bad = report()
        bad["modules"]["album"]["needs_download"] = 1
        cases.append(bad)
        bad = report()
        bad["modules"]["album"]["state"] = "permission_required"
        bad["modules"]["album"]["permission"] = "denied"
        cases.append(bad)
        for value in cases:
            with self.assertRaises(Refused):
                self.link.report(key, value)
        self.assertEqual(self.events, before)
        self.assertEqual(self.link.active["sequence"], 0)

    def test_duplicate_json_keys_and_non_finite_rejected(self):
        for raw in [b'{"protocol":1,"protocol":2}', b'{"value":NaN}', b"[]\xff", b"{"]:
            with self.assertRaises(Refused):
                decode(raw)

    def test_wrong_token_cancel_cannot_revoke(self):
        key = self.paired()
        with self.assertRaises(Refused):
            self.link.cancel("different")
        self.assertTrue(self.link.report(key, report())["accepted"])
        self.link.cancel(key)
        with self.assertRaises(Refused):
            self.link.report(key, report(2))

    def test_bounded_start_approval_and_report_rate(self):
        for _ in range(5):
            self.link.start(POLICY)
        with self.assertRaises(Refused):
            self.link.start(POLICY)
        for _ in range(5):
            with self.assertRaises(Refused):
                self.link.approve("wrong")
        with self.assertRaises(Refused):
            self.link.approve(self.link.pending["code"])
        self.now = 61
        key = self.paired()
        for sequence in range(1, 61):
            self.link.report(key, report(sequence))
        with self.assertRaises(Refused):
            self.link.report(key, report(61))

    def test_restart_drops_authorization(self):
        key = self.paired()
        fresh = Link(lambda *_: None)
        with self.assertRaises(Refused):
            fresh.report(key, report())


if __name__ == "__main__":
    unittest.main()
