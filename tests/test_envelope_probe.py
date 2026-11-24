import unittest

import envelope_probe


class EnvelopeProbeTest(unittest.TestCase):
    def test_small_envelope_survives_every_fault_case(self):
        summary = envelope_probe.run(envelope_probe.parse([
            "--executions", "1000", "--due-waits", "200", "--fanout", "60", "--throttled", "2",
            "--captains", "2", "--max-writers", "2", "--duration", "20", "--throttle-seconds", "4",
            "--outage-seconds", "3", "--aging-seconds", "10",
        ]))
        self.assertEqual(summary["violations"], [])
        self.assertEqual(summary["restart"]["due_rechecks_admitted"], 200)
        self.assertEqual(summary["invariants"]["fanout_still_blocked"], 0)
        self.assertEqual(summary["outage"]["health_status"], 503)
        self.assertNotIn(500, summary["outage"]["server_error_statuses"])
        self.assertEqual(summary["throttle"]["claimed_before_retry_at"], 0)
        self.assertEqual(summary["interrupt_and_prerequisite"]["interrupted_resumed_stage"], "report_review")
        self.assertTrue(summary["interrupt_and_prerequisite"]["interrupted_kept_draft"])


if __name__ == "__main__":
    unittest.main()
