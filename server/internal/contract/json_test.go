package contract

import (
	"bytes"
	"encoding/json"
	"io"
	"reflect"
	"strings"
	"testing"
	"time"
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

func TestJSONValueMatchesPreviousResponseRoundTrip(t *testing.T) {
	type view struct {
		Text    string          `json:"text"`
		Count   int64           `json:"count"`
		State   json.RawMessage `json:"state"`
		Empty   []string        `json:"empty"`
		Absent  []string        `json:"absent"`
		Omitted *string         `json:"omitted,omitempty"`
		When    AwareTime       `json:"when"`
	}
	type Embedded struct {
		ID string `json:"id"`
	}
	value := struct {
		Embedded
		View view `json:"view"`
	}{
		Embedded: Embedded{"outer"},
		View: view{Text: "<>&\u00e9\U0001f600\u2028\n", Count: 9007199254740993,
			State: json.RawMessage(`{"float":1e0,"small":0.00001,"zero":-0.0,"huge":999999999999999999999999999999}`),
			Empty: []string{}, When: AwareTime{time.Date(2026, 11, 27, 1, 2, 3, 123456000, time.UTC)}},
	}
	previous, err := Canonical(value)
	if err != nil {
		t.Fatal(err)
	}
	old, _, err := Decode([]byte(previous))
	if err != nil {
		t.Fatal(err)
	}
	projected, err := JSONValue(value)
	if err != nil {
		t.Fatal(err)
	}
	wire := func(value any) []byte {
		t.Helper()
		var buffer bytes.Buffer
		if err := json.NewEncoder(&buffer).Encode(value); err != nil {
			t.Fatal(err)
		}
		return buffer.Bytes()
	}
	if !bytes.Equal(wire(old), wire(projected)) {
		t.Fatalf("wire changed:\nold %s\nnew %s", wire(old), wire(projected))
	}
	persisted, err := Canonical(projected)
	if err != nil || persisted != previous {
		t.Fatal("canonical receipt snapshot changed", persisted, previous, err)
	}
}

func TestOrderedJSONValuePreservesStructOrder(t *testing.T) {
	value := struct {
		Z string `json:"z"`
		A []struct {
			Y int `json:"y"`
			B int `json:"b"`
		} `json:"a"`
	}{Z: "first"}
	value.A = append(value.A, struct {
		Y int `json:"y"`
		B int `json:"b"`
	}{1, 2})
	projected, err := OrderedJSONValue(value)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(ObjectKeys(projected), []string{"z", "a"}) {
		t.Fatal("struct field order changed", ObjectKeys(projected))
	}
	old, err := PythonJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	current, err := PythonJSON(projected)
	if err != nil || current != old {
		t.Fatal("repr/storage field order changed", current, old, err)
	}
}

func BenchmarkResponseProjection(b *testing.B) {
	type row struct {
		ID       string          `json:"id"`
		Version  int64           `json:"version"`
		State    json.RawMessage `json:"state"`
		Sessions []string        `json:"sessions"`
	}
	rows := make([]row, 100)
	for index := range rows {
		rows[index] = row{"execution", int64(index),
			json.RawMessage(`{"stage":"development","status":"running","target":"checked_in","candidate":null}`),
			[]string{"writer", "captain"}}
	}
	for _, previous := range []bool{true, false} {
		name := "values"
		if previous {
			name = "encode-decode"
		}
		b.Run(name, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				var value any
				var err error
				if previous {
					var text string
					text, err = Canonical(rows)
					if err == nil {
						value, _, err = Decode([]byte(text))
					}
				} else {
					value, err = JSONValue(rows)
				}
				if err != nil {
					b.Fatal(err)
				}
				if err := json.NewEncoder(io.Discard).Encode(value); err != nil {
					b.Fatal(err)
				}
			}
		})
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
