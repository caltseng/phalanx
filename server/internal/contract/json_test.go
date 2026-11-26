package contract

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestCanonicalPythonNumbers(t *testing.T) {
	for _, test := range []struct{ input, expected string }{
		{"-0", "0"}, {"-0.0", "-0.0"}, {"0e0", "0.0"},
		{"1.0", "1.0"}, {"1e0", "1.0"}, {"0.0001", "0.0001"},
		{"0.00001", "1e-05"}, {"1e15", "1000000000000000.0"}, {"1e16", "1e+16"},
		{"999999999999999999999999999999999999", "999999999999999999999999999999999999"},
		{"5e-324", "5e-324"}, {"1.7976931348623157e308", "1.7976931348623157e+308"},
		{"2.2250738585072014e-308", "2.2250738585072014e-308"},
		{"1.234567890123456789", "1.2345678901234567"},
	} {
		actual, err := Canonical(json.Number(test.input))
		if err != nil || actual != test.expected {
			t.Fatalf("%s: %q != %q (%v)", test.input, actual, test.expected, err)
		}
	}
	for _, input := range []string{"1e400", "-1e400", "NaN", "Infinity"} {
		if _, err := Canonical(json.Number(input)); err == nil {
			t.Fatal("nonfinite or invalid number serialized", input)
		}
	}
}

func TestCanonicalNestedNumericReceiptMaterial(t *testing.T) {
	input := `{"snapshot":{"units":[0.1,2.0,-0.0,1000000000000000000000000000000]},"writer_id":"fixture"}`
	value, _, err := Decode([]byte(input))
	if err != nil {
		t.Fatal(err)
	}
	actual, err := Canonical(value)
	if err != nil || !strings.Contains(actual, `[0.1,2.0,-0.0,1000000000000000000000000000000]`) {
		t.Fatal("typed receipt numbers normalized or lost", actual, err)
	}
}
