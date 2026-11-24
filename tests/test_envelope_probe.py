import unittest
from pathlib import Path
from unittest.mock import Mock

import envelope_probe


class EnvelopeProbeTest(unittest.TestCase):
    def test_lost_terminal_reply_requires_exact_committed_result_before_confirmation(self):
        for case in ("committed", "other-attempt", "other-revision", "wrong-version", "not-completed"):
            with self.subTest(case=case):
                probe = envelope_probe.Probe(envelope_probe.parse([]), Path("unused"))
                claim = {
                    "item_id": "item", "attempt_id": "attempt",
                    "execution": {"id": "execution", "version": 6,
                                  "state": {"stage": "report_storage", "report_draft": "revision"}},
                }
                event = {
                    "event": "result", "attempt_id": "attempt", "version": 7,
                    "details": {"stage": "report_storage", "outcome": "succeeded",
                                "report_revision": "revision"},
                }
                completed = {
                    "id": "execution",
                    "state": {"status": "completed", "report_draft": "revision"},
                }
                if case == "other-attempt":
                    event["attempt_id"] = "other"
                elif case == "other-revision":
                    event["details"]["report_revision"] = "other"
                elif case == "wrong-version":
                    event["version"] = 9
                elif case == "not-completed":
                    completed["state"]["status"] = "waiting_for_external"
                client = Mock()
                client.call.side_effect = [
                    (401, {"detail": "Valid bearer token required"}),
                    (200, [event]), (200, {"execution": completed}),
                ]
                result = probe.finish_stage(client, claim, "ended-token")
                if case == "committed":
                    self.assertEqual(result, completed)
                    self.assertEqual(probe.violations, [])
                    self.assertEqual(probe.metrics.counts["completed_results_confirmed_after_401"], 1)
                    self.assertEqual(client.call.call_args_list[1].args[1:4], (
                        "GET", "/v1/items/item/executions/execution/timeline", envelope_probe.OWNER,
                    ))
                else:
                    self.assertIsNone(result)
                    self.assertIn("returned 401", probe.violations[0])

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
