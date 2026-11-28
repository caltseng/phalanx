package contract

import "encoding/json"

type Reply struct {
	ExpectedVersion json.Number `json:"expected_version"`
	Answer          string      `json:"answer"`
	AnsweredBy      string      `json:"answered_by"`
}

func ValidateReply(value any, keys []string) (Reply, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Reply{}, modelIssue(prefix)
	}
	v, result := validator{}, Reply{AnsweredBy: "owner"}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	if x, ok := v.required(body, "answer", prefix); ok {
		result.Answer = v.text(x, location(prefix, "answer"), true, 1, 16000, "")
	}
	if x, ok := body["answered_by"]; ok {
		result.AnsweredBy = v.choice(x, location(prefix, "answered_by"), []string{"owner", "captain"})
	}
	v.extras(keys, []string{"expected_version", "answer", "answered_by"}, prefix)
	return result, v.issues
}
