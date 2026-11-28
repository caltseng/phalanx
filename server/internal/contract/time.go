package contract

import (
	"encoding/json"
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

func (v *validator) timestamp(value any, at []any) Timestamp {
	text, ok := value.(string)
	if ok {
		if parsed, err := time.Parse(time.RFC3339Nano, text); err == nil {
			return Timestamp{parsed.Truncate(time.Microsecond)}
		}
	}
	v.issue("Input must be an RFC3339 timestamp with a timezone", at)
	return Timestamp{}
}
