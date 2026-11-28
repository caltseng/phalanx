"""Native runtime checks cannot establish expectations from their own responses."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "conformance"))
import headers
import release


class NativeRuntimeTest(unittest.TestCase):
    def test_default_is_native_and_historical_mode_retains_both_references(self):
        self.assertEqual(release.load_implementations(), ("Go",))
        self.assertEqual(release.load_implementations(True), ("P0", "D0", "Go"))

    def test_native_boundary_rejects_wrong_response_even_when_repeated(self):
        case = next(case for case in headers.cases() if case[0] == "fixed-body-complete")
        expected = headers.native_header(case)
        actual = {**deepcopy(expected), "status": 200}
        refusal = actual["responses"][1]
        actual["responses"][1] = (refusal[0], 200, *refusal[2:])
        self.assertTrue(headers.header_differences([expected], [actual]))
        actual = {**deepcopy(expected), "status": 200, "closed": False}
        self.assertTrue(headers.header_differences([expected], [actual]))

    def test_native_refusal_checks_exact_wire_and_zero_effects(self):
        case = next(case for case in headers.cases() if case[0] == "refused-intake-receipt")
        expected = headers.native_header(case)
        actual = {**deepcopy(expected), "status": 400}
        self.assertEqual(headers.header_differences([expected], [actual]), [])
        for field, changed in (("raw_response", expected["raw_response"][:-1]),
                               ("durable_unchanged", False), ("early", False)):
            with self.subTest(field=field):
                self.assertTrue(headers.header_differences([expected], [{**actual, field: changed}]))

    def test_independent_expectation_matches_real_raw_parser_shape(self):
        case = next(case for case in headers.cases() if case[0] == "refused-intake-receipt")
        expected = headers.native_header(case)
        raw = (b"HTTP/1.1 400 Bad Request\r\ncontent-type: text/plain; charset=utf-8\r\n"
               b"Connection: close\r\n\r\nInvalid HTTP request received.")
        actual = {**expected, "responses": headers.comparable_responses(raw),
                  "raw_response": raw.decode("latin1"), "status": 400}
        self.assertEqual(headers.header_differences([expected], [actual]), [])
        first = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nContent-Length: 15\r\n\r\n"
                 b'{"status":"ok"}')
        case = next(case for case in headers.cases() if case[0] == "fixed-body-complete")
        self.assertEqual(headers.comparable_responses(first + raw), headers.native_header(case)["responses"])

    def test_native_framing_checks_payload_protocol_and_closure(self):
        case = next(case for case in headers.framework.cases() if case["id"] == "http10-invalid-body")
        expected = headers.native_framing(case, {})
        response = {"protocol": expected[0], "status": expected[1], "headers": expected[2],
                    "decoded_body_hex": expected[3], "closed": expected[4]}
        self.assertEqual(headers.framework.comparable(response), expected)
        for field, changed in (("closed", False), ("protocol", "HTTP/1.0"),
                               ("decoded_body_hex", b'{"detail":[]}'.hex())):
            with self.subTest(field=field):
                self.assertNotEqual(headers.framework.comparable({**response, field: changed}), expected)
        changed_case = {**case, "raw": case["raw"] + " "}
        with self.assertRaisesRegex(AssertionError, "Unclassified"):
            headers.native_framing(changed_case, {})

    def test_native_framing_does_not_ignore_new_cases(self):
        new_case = ("new-case", 0, None, False, False)
        with patch.object(headers, "cases", return_value=[new_case]):
            with self.assertRaisesRegex(AssertionError, "Unclassified"):
                headers.native_header(new_case)


if __name__ == "__main__":
    unittest.main()
