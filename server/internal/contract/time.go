package contract

import (
	"encoding/json"
	"regexp"
	"time"
)

type Timestamp struct {
	time.Time
}

func (value Timestamp) MarshalJSON() ([]byte, error) {
	text, err := value.JSONValue()
	if err != nil {
		return nil, err
	}
	return json.Marshal(text)
}

func (value Timestamp) JSONValue() (any, error) {
	// Receipt material predates the native server. Keep its microsecond
	// timestamp spelling even though request parsing is now RFC3339 only.
	layout := "2006-01-02T15:04:05"
	if value.Nanosecond() != 0 {
		layout += ".000000"
	}
	return value.Format(layout + "Z07:00"), nil
}

func ValidateTimestamp(value any, at []any) (Timestamp, []Issue) {
	v := validator{}
	result := v.timestamp(value, at)
	return result, v.issues
}

// time.Parse accepts single-digit hours, comma fractions and out-of-range zone
// components. Check the wire syntax before letting it validate the calendar.
var timestampSyntax = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$`)

func (v *validator) timestamp(value any, at []any) Timestamp {
	text, ok := value.(string)
	if ok && timestampSyntax.MatchString(text) {
		if parsed, err := time.Parse(time.RFC3339Nano, text); err == nil && parsed.Year() >= 1 {
			return Timestamp{parsed.Truncate(time.Microsecond)}
		}
	}
	v.issue("Input must be an RFC3339 timestamp with a timezone and year 0001-9999", at)
	return Timestamp{}
}
