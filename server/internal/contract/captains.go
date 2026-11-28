package contract

import (
	"encoding/json"
	"fmt"
	"math/big"
)

type Pickup struct {
	Repositories map[string]*string `json:"repositories"`
}

type CaptainRegistration struct {
	SessionRef      string  `json:"session_ref"`
	CharterRevision string  `json:"charter_revision"`
	ParentID        *string `json:"parent_id"`
	Pickup          Pickup  `json:"pickup"`
}

type NativeSession struct {
	ID           string  `json:"id"`
	Name         string  `json:"name"`
	WriterNonce  *string `json:"writer_nonce,omitempty"`
	Availability *string `json:"availability,omitempty"`
	Runtime      *string `json:"runtime,omitempty"`
}

type CaptainHeartbeat struct {
	CharterRevision string
	WakeCount       json.Number
	CompactionCount *json.Number
	State           string
	Focus           []string
	Sessions        []NativeSession
	HandoffNote     *string
	Pickup          *Pickup
}

type CaptainAction struct {
	CaptainID string `json:"captain_id"`
	Action    string `json:"action"`
	Reason    string `json:"reason"`
}

type Reinforcement struct {
	Interval json.Number
}

func (v *validator) pickup(value any, prefix []any) Pickup {
	result := Pickup{Repositories: map[string]*string{}}
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(prefix)...)
		return result
	}
	if input, exists := body["repositories"]; exists {
		entries, ok := object(input)
		at := location(prefix, "repositories")
		if !ok {
			v.issue("Input should be a valid dictionary", at)
		} else {
			start := len(v.issues)
			for _, key := range ObjectKeys(input) {
				v.text(key, location(location(at, key), "[key]"), false, 0, 0, IdentifierPattern)
				var text *string
				if entries[key] != nil {
					value := v.text(entries[key], location(at, key), true, 1, 16000, "")
					text = &value
				}
				result.Repositories[key] = text
			}
			if len(v.issues) == start && len(entries) > 100 {
				v.issue(fmt.Sprintf("Dictionary should have at most 100 items after validation, not %d", len(entries)),
					at)
			}
		}
	}
	v.extras(ObjectKeys(value), []string{"repositories"}, prefix)
	return result
}

func ValidateCaptainRegistration(value any, keys []string) (CaptainRegistration, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return CaptainRegistration{}, modelIssue(prefix)
	}
	v := validator{}
	result := CaptainRegistration{Pickup: Pickup{Repositories: map[string]*string{}}}
	if value, exists := v.required(body, "session_ref", prefix); exists {
		result.SessionRef = v.text(value, location(prefix, "session_ref"), true, 1, 16000, "")
	}
	if value, exists := v.required(body, "charter_revision", prefix); exists {
		result.CharterRevision = v.text(value, location(prefix, "charter_revision"), true, 1, 200, "")
	}
	if value, exists := body["parent_id"]; exists && value != nil {
		text := v.text(value, location(prefix, "parent_id"), false, 0, 0, IdentifierPattern)
		result.ParentID = &text
	}
	if value, exists := body["pickup"]; exists {
		result.Pickup = v.pickup(value, location(prefix, "pickup"))
	}
	v.extras(keys, []string{"session_ref", "charter_revision", "parent_id", "pickup"}, prefix)
	return result, v.issues
}

func (v *validator) nativeSession(value any, at []any) NativeSession {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return NativeSession{}
	}
	result := NativeSession{}
	if value, exists := v.required(body, "id", at); exists {
		result.ID = v.text(value, location(at, "id"), true, 1, 200, "")
	}
	if value, exists := v.required(body, "name", at); exists {
		result.Name = v.text(value, location(at, "name"), true, 1, 200, "")
	}
	if value, exists := body["writer_nonce"]; exists && value != nil {
		text := v.text(value, location(at, "writer_nonce"), true, 1, 200, "")
		result.WriterNonce = &text
	}
	if value, exists := body["availability"]; exists && value != nil {
		text := v.choice(value, location(at, "availability"), []string{"present", "missing", "deleted"})
		result.Availability = &text
	}
	if value, exists := body["runtime"]; exists && value != nil {
		text := v.text(value, location(at, "runtime"), true, 1, 200, "")
		result.Runtime = &text
	}
	v.extras(ObjectKeys(value), []string{"id", "name", "writer_nonce", "availability", "runtime"}, at)
	return result
}

func ValidateCaptainHeartbeat(value any, keys []string) (CaptainHeartbeat, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return CaptainHeartbeat{}, modelIssue(prefix)
	}
	v := validator{}
	result := CaptainHeartbeat{Focus: []string{}, Sessions: []NativeSession{}}
	if value, exists := v.required(body, "charter_revision", prefix); exists {
		result.CharterRevision = v.text(value, location(prefix, "charter_revision"), true, 1, 200, "")
	}
	if value, exists := v.required(body, "wake_count", prefix); exists {
		result.WakeCount = v.strictInteger(value, location(prefix, "wake_count"), 0)
	}
	if value, exists := body["compaction_count"]; exists && value != nil {
		number := v.strictInteger(value, location(prefix, "compaction_count"), 0)
		integer, _ := new(big.Int).SetString(string(number), 10)
		if integer.Cmp(big.NewInt(9007199254740991)) > 0 {
			v.issue("Input should be less than or equal to 9007199254740991",
				location(prefix, "compaction_count"))
		}
		result.CompactionCount = &number
	}
	if value, exists := v.required(body, "state", prefix); exists {
		result.State = v.choice(value, location(prefix, "state"), []string{"active", "ending", "ended"})
	}
	if value, exists := body["focus"]; exists {
		at := location(prefix, "focus")
		values, ok := value.([]any)
		if !ok {
			v.issue("Input should be a valid list", at)
		} else {
			start := len(v.issues)
			for index, entry := range values {
				result.Focus = append(result.Focus, v.text(entry, location(at, index), false, 0, 0, IdentifierPattern))
			}
			if len(v.issues) == start && len(values) > 100 {
				v.tooLong(at, 100, len(values))
			}
		}
	}
	if value, exists := body["sessions"]; exists {
		at := location(prefix, "sessions")
		values, ok := value.([]any)
		if !ok {
			v.issue("Input should be a valid list", at)
		} else {
			start := len(v.issues)
			for index, entry := range values {
				result.Sessions = append(result.Sessions, v.nativeSession(entry, location(at, index)))
			}
			if len(v.issues) == start && len(values) > 500 {
				v.tooLong(at, 500, len(values))
			}
		}
	}
	result.HandoffNote = v.optionalText(body, "handoff_note", prefix)
	if value, exists := body["pickup"]; exists && value != nil {
		pickup := v.pickup(value, location(prefix, "pickup"))
		result.Pickup = &pickup
	}
	v.extras(keys, []string{"charter_revision", "wake_count", "compaction_count", "state", "focus", "sessions", "handoff_note", "pickup"}, prefix)
	return result, v.issues
}

func ValidateCaptainAction(value any, keys []string) (CaptainAction, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return CaptainAction{}, modelIssue(prefix)
	}
	v, result := validator{}, CaptainAction{}
	if value, exists := v.required(body, "captain_id", prefix); exists {
		result.CaptainID = v.text(value, location(prefix, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	if value, exists := v.required(body, "action", prefix); exists {
		result.Action = v.text(value, location(prefix, "action"), true, 1, 200, "")
	}
	if value, exists := v.required(body, "reason", prefix); exists {
		result.Reason = v.text(value, location(prefix, "reason"), true, 1, 16000, "")
	}
	v.extras(keys, []string{"captain_id", "action", "reason"}, prefix)
	return result, v.issues
}

func ValidateReinforcement(value any, keys []string) (Reinforcement, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Reinforcement{}, modelIssue(prefix)
	}
	v, result := validator{}, Reinforcement{}
	if value, exists := v.required(body, "interval", prefix); exists {
		result.Interval = v.strictInteger(value, location(prefix, "interval"), 1)
	}
	v.extras(keys, []string{"interval"}, prefix)
	return result, v.issues
}
