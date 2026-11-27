package contract

import "net/url"

type SessionArchive struct {
	SessionID  string `json:"session_id"`
	Outcome    string `json:"outcome"`
	Reason     string `json:"reason"`
	ReportedBy string `json:"reported_by"`
}

func ValidateArchive(value any, keys []string) (SessionArchive, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return SessionArchive{}, modelIssue(value, prefix)
	}
	v, result := validator{}, SessionArchive{}
	if value, exists := v.required(body, "session_id", prefix); exists {
		result.SessionID = v.text(value, location(prefix, "session_id"), true, 1, 200, "")
	}
	if value, exists := v.required(body, "outcome", prefix); exists {
		result.Outcome = v.choice(value, location(prefix, "outcome"), []string{"archived", "blocked", "refused"}, true)
	}
	if value, exists := v.required(body, "reason", prefix); exists {
		result.Reason = v.text(value, location(prefix, "reason"), true, 1, 16000, "")
	}
	if value, exists := v.required(body, "reported_by", prefix); exists {
		result.ReportedBy = v.text(value, location(prefix, "reported_by"), true, 1, 200, "")
	}
	v.extras(body, keys, []string{"session_id", "outcome", "reason", "reported_by"}, prefix)
	return result, v.issues
}

func ValidateArchiveQuery(values url.Values) (string, []Issue) {
	v := validator{}
	value, present := lastQuery(values, "session_id")
	if !present {
		v.issue("missing", "Field required", []any{"query", "session_id"}, nil, nil)
		return "", v.issues
	}
	return v.text(value, []any{"query", "session_id"}, true, 1, 200, ""), v.issues
}
