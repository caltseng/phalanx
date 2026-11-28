package contract

import (
	"encoding/base64"
	"testing"
)

func TestOperatorCursorRoundTripAndValidation(t *testing.T) {
	scope := `open:` + Digest("q=%E9%9B%AA")
	shape := []string{"rank", "stamp", "id"}
	key := []any{0, "2026-01-02T03:04:05.123456+00:00", "0123456789abcdef0123456789abcdef"}
	cursor, err := EncodeCursor(scope, key)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeCursor(&cursor, scope, shape)
	if err != nil {
		t.Fatal(err)
	}
	reencoded, err := EncodeCursor(scope, decoded)
	if err != nil || reencoded != cursor {
		t.Fatal("cursor did not round trip", reencoded, err)
	}
	for _, test := range []struct {
		name, cursor, scope string
	}{
		{"other filters", cursor, "open:" + Digest("q=other")},
		{"other kind", cursor, "closed"},
		{"padding", cursor + "=", scope},
		{"newline", cursor + "\n", scope},
		{"malformed", base64.RawURLEncoding.EncodeToString([]byte(`["1","open",[]]`)), "open"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := DecodeCursor(&test.cursor, test.scope, shape); err == nil {
				t.Fatal("invalid cursor accepted")
			}
		})
	}
}
