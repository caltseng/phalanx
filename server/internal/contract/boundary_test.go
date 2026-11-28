package contract

import (
	"encoding/json"
	"net/url"
	"reflect"
	"testing"
)

func TestNativeJSONBoundary(t *testing.T) {
	value, keys, err := Decode([]byte(`{"goal":"old","number":9007199254740993,"goal":"new"}`))
	if err != nil {
		t.Fatal(err)
	}
	object, ok := value.(map[string]any)
	if !ok || object["goal"] != "new" || object["number"] != json.Number("9007199254740993") ||
		!reflect.DeepEqual(keys, []string{"goal", "number"}) {
		t.Fatal(value, keys)
	}
	for _, invalid := range []string{"", `{"goal":}`, `{} {}`, `{} null`, `{"value":NaN}`, `{"value":Infinity}`, "\xef\xbb\xbf{}"} {
		if _, _, err := Decode([]byte(invalid)); err == nil {
			t.Errorf("accepted invalid JSON: %q", invalid)
		}
	}
	// encoding/json's replacement behavior must not revive Python's surrogate
	// sentinel, which used to turn validation input into an HTTP 500.
	value, _, err = Decode([]byte(`{"goal":"\ud800"}`))
	if err != nil || value.(map[string]any)["goal"] != "\ufffd" {
		t.Fatal(value, err)
	}
}

func TestNativeIntegerParameters(t *testing.T) {
	for _, invalid := range []string{"1_0", "10.0", " 10 ", "1e1", "9223372036854775808", "0", "101"} {
		if _, issues := ValidateLimit(url.Values{"limit": {invalid}}, 25); len(issues) != 1 ||
			!reflect.DeepEqual(issues[0].Loc, []any{"query", "limit"}) {
			t.Errorf("accepted invalid limit %q: %v", invalid, issues)
		}
	}
	if limit, issues := ValidateLimit(url.Values{"limit": {"1", "100"}}, 25); limit != 100 || len(issues) != 0 {
		t.Fatal(limit, issues)
	}
	if limit, issues := ValidateLimit(nil, 25); limit != 25 || len(issues) != 0 {
		t.Fatal(limit, issues)
	}
}

func TestRFC3339TimestampsAndReceiptSpelling(t *testing.T) {
	for _, test := range []struct{ input, expected string }{
		{"2026-11-27T12:34:56Z", `"2026-11-27T12:34:56Z"`},
		{"2026-11-27T12:34:56.1234567+01:30", `"2026-11-27T12:34:56.123456+01:30"`},
		{"2026-11-27T12:34:56.1+00:00", `"2026-11-27T12:34:56.100000Z"`},
	} {
		value, issues := ValidateTimestamp(test.input, []any{"body", "observed_at"})
		if len(issues) != 0 {
			t.Fatal(issues)
		}
		actual, err := Canonical(value)
		if err != nil || actual != test.expected {
			t.Fatal(actual, test.expected, err)
		}
	}
	for _, invalid := range []any{nil, true, json.Number("1700000000"), "1700000000", "2026-11-27",
		"2026-11-27 12:34:56+00:00", "2026-11-27T12:34:56", "2026-02-30T12:34:56Z"} {
		if _, issues := ValidateTimestamp(invalid, []any{"body", "observed_at"}); len(issues) != 1 {
			t.Errorf("accepted invalid timestamp %v", invalid)
		}
	}
}
