package contract

import (
	"encoding/json"
	"math"
	"math/big"
	"strconv"
	"strings"
)

const NoncePattern = `^[A-Za-z0-9_-]{16,100}$`
const URLPattern = `^[a-z][a-z0-9+.-]{0,31}://\S{1,2000}$`
const SHAPattern = `^[0-9a-f]{40}$`

type WriterRegistration struct {
	ExpectedVersion json.Number `json:"expected_version"`
	CaptainID       string      `json:"captain_id"`
	Runtime         string      `json:"runtime"`
	Nonce           string      `json:"nonce"`
	TokenDigest     string      `json:"token_digest"`
	Replaces        *string     `json:"replaces"`
	ReplacedStopped bool        `json:"replaced_stopped"`
}

type WriterUpdate struct {
	CaptainID  string  `json:"captain_id"`
	WriterID   string  `json:"writer_id"`
	SessionID  *string `json:"session_id"`
	SessionURL *string `json:"session_url"`
	State      *string `json:"state"`
	Abandon    bool    `json:"abandon"`
}

type WriterWithdrawal struct {
	CaptainID string `json:"captain_id"`
	WriterID  string `json:"writer_id"`
	Reason    string `json:"reason"`
	Stopped   bool   `json:"stopped"`
}

type WriterProgress struct {
	Note *string `json:"note"`
}

type WriterCapacity struct {
	Phase            string      `json:"phase"`
	ExpectedRevision json.Number `json:"expected_revision"`
	HandoffHead      *string     `json:"handoff_head"`
}

func (v *validator) boolean(value any, loc []any, strict bool) bool {
	if result, ok := value.(bool); ok {
		return result
	}
	kind := "bool_type"
	if !strict {
		switch input := value.(type) {
		case string:
			switch strings.ToLower(input) {
			case "1", "true", "t", "yes", "y", "on":
				return true
			case "0", "false", "f", "no", "n", "off":
				return false
			}
			kind = "bool_parsing"
		case json.Number:
			if strings.ContainsAny(string(input), ".eE") || input == "NaN" || input == "Infinity" || input == "-Infinity" {
				n, err := strconv.ParseFloat(string(input), 64)
				if err == nil && !math.IsNaN(n) && !math.IsInf(n, 0) && math.Trunc(n) == n && n >= -9223372036854775808.0 && n < 9223372036854775808.0 {
					if n == 0 || n == 1 {
						return n == 1
					}
					kind = "bool_parsing"
				}
			} else if integer, ok := new(big.Int).SetString(string(input), 10); ok && integer.IsInt64() {
				n := integer.Int64()
				if n == 0 || n == 1 {
					return n == 1
				}
				kind = "bool_parsing"
			}
		}
	}
	message := "Input should be a valid boolean"
	if kind == "bool_parsing" {
		message += ", unable to interpret input"
	}
	v.issue(kind, message, loc, value, nil)
	return false
}

func ValidateWriterRegistration(value any, keys []string) (WriterRegistration, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterRegistration{}, modelIssue(value, prefix)
	}
	v, result := validator{}, WriterRegistration{}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	if x, ok := v.required(body, "captain_id", prefix); ok {
		result.CaptainID = v.text(x, location(prefix, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "runtime", prefix); ok {
		result.Runtime = v.text(x, location(prefix, "runtime"), true, 1, 200, "")
	}
	if x, ok := v.required(body, "nonce", prefix); ok {
		result.Nonce = v.text(x, location(prefix, "nonce"), false, 0, 0, NoncePattern)
	}
	if x, ok := v.required(body, "token_digest", prefix); ok {
		result.TokenDigest = v.text(x, location(prefix, "token_digest"), false, 0, 0, RevisionPattern)
	}
	if x, ok := body["replaces"]; ok && x != nil {
		text := v.text(x, location(prefix, "replaces"), false, 0, 0, IdentifierPattern)
		result.Replaces = &text
	}
	if x, ok := body["replaced_stopped"]; ok {
		result.ReplacedStopped = v.boolean(x, location(prefix, "replaced_stopped"), false)
	}
	v.extras(body, keys, []string{"expected_version", "captain_id", "runtime", "nonce", "token_digest", "replaces", "replaced_stopped"}, prefix)
	return result, v.issues
}

func ValidateWriterUpdate(value any, keys []string) (WriterUpdate, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterUpdate{}, modelIssue(value, prefix)
	}
	v, result := validator{}, WriterUpdate{}
	if x, ok := v.required(body, "captain_id", prefix); ok {
		result.CaptainID = v.text(x, location(prefix, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "writer_id", prefix); ok {
		result.WriterID = v.text(x, location(prefix, "writer_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := body["session_id"]; ok && x != nil {
		text := v.text(x, location(prefix, "session_id"), true, 1, 200, "")
		result.SessionID = &text
	}
	if x, ok := body["session_url"]; ok && x != nil {
		text := v.text(x, location(prefix, "session_url"), false, 0, 0, URLPattern)
		result.SessionURL = &text
	}
	if x, ok := body["state"]; ok && x != nil {
		text := v.choice(x, location(prefix, "state"), []string{"busy", "idle", "stopped", "error"}, true)
		result.State = &text
	}
	if x, ok := body["abandon"]; ok {
		result.Abandon = v.boolean(x, location(prefix, "abandon"), false)
	}
	v.extras(body, keys, []string{"captain_id", "writer_id", "session_id", "session_url", "state", "abandon"}, prefix)
	return result, v.issues
}

func ValidateWriterWithdrawal(value any, keys []string) (WriterWithdrawal, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterWithdrawal{}, modelIssue(value, prefix)
	}
	v, result := validator{}, WriterWithdrawal{}
	if x, ok := v.required(body, "captain_id", prefix); ok {
		result.CaptainID = v.text(x, location(prefix, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "writer_id", prefix); ok {
		result.WriterID = v.text(x, location(prefix, "writer_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "reason", prefix); ok {
		result.Reason = v.text(x, location(prefix, "reason"), true, 1, 16000, "")
	}
	if x, ok := body["stopped"]; ok {
		result.Stopped = v.boolean(x, location(prefix, "stopped"), false)
	}
	v.extras(body, keys, []string{"captain_id", "writer_id", "reason", "stopped"}, prefix)
	return result, v.issues
}

func ValidateWriterProgress(value any, keys []string) (WriterProgress, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterProgress{}, modelIssue(value, prefix)
	}
	v, result := validator{}, WriterProgress{}
	if x, ok := body["note"]; ok && x != nil {
		text := v.text(x, location(prefix, "note"), true, 1, 16000, "")
		result.Note = &text
	}
	v.extras(body, keys, []string{"note"}, prefix)
	return result, v.issues
}

func ValidateWriterCapacity(value any, keys []string) (WriterCapacity, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterCapacity{}, modelIssue(value, prefix)
	}
	v, result := validator{}, WriterCapacity{}
	if x, ok := v.required(body, "phase", prefix); ok {
		result.Phase = v.choice(x, location(prefix, "phase"), []string{"building", "delivering"}, true)
	}
	if x, ok := v.required(body, "expected_revision", prefix); ok {
		result.ExpectedRevision = v.strictInteger(x, location(prefix, "expected_revision"), 0)
	}
	if x, ok := body["handoff_head"]; ok && x != nil {
		text := v.text(x, location(prefix, "handoff_head"), false, 0, 0, SHAPattern)
		result.HandoffHead = &text
	}
	v.extras(body, keys, []string{"phase", "expected_revision", "handoff_head"}, prefix)
	return result, v.issues
}
