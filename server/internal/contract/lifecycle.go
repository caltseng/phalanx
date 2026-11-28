package contract

import "encoding/json"

type Versioned struct {
	ExpectedVersion json.Number `json:"expected_version"`
}

type Cancellation struct {
	ExpectedVersion json.Number `json:"expected_version"`
	Reason          string      `json:"reason"`
}

func ValidateVersioned(value any, keys []string) (Versioned, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Versioned{}, modelIssue(prefix)
	}
	v := validator{}
	result := Versioned{}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	v.extras(keys, []string{"expected_version"}, prefix)
	return result, v.issues
}

func ValidateCancellation(value any, keys []string) (Cancellation, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Cancellation{}, modelIssue(prefix)
	}
	v := validator{}
	result := Cancellation{}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	if x, ok := v.required(body, "reason", prefix); ok {
		result.Reason = v.text(x, location(prefix, "reason"), true, 1, 16000, "")
	}
	v.extras(keys, []string{"expected_version", "reason"}, prefix)
	return result, v.issues
}
