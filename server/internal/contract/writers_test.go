package contract

import (
	"strings"
	"testing"
)

func TestWriterSessionURLUsesUnicodeWhitespaceAndExactTailBound(t *testing.T) {
	for _, test := range []struct {
		tail string
		ok   bool
	}{
		{"native-session", true},
		{strings.Repeat("x", 2000), true},
		{strings.Repeat("x", 2001), false},
		{"native\u00a0session", false},
		{"native\u2009session", false},
		{"native\u001csession", true},
	} {
		input := map[string]any{"captain_id": "captain", "writer_id": "writer", "session_url": "runtime://" + test.tail}
		_, issues := ValidateWriterUpdate(input, []string{"captain_id", "writer_id", "session_url"})
		if (len(issues) == 0) != test.ok {
			t.Fatalf("URL tail length=%d: %v", len(test.tail), issues)
		}
	}
}
