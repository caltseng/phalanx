package contract

import (
	"encoding/json"
	"math/big"
	"strconv"
)

const ScoutLoginPattern = `^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`
const ScoutAuthorPattern = `^[A-Za-z0-9][A-Za-z0-9-]{0,92}(?:\[bot\])?$`

type ScoutSettings struct {
	ExpectedRevision json.Number `json:"expected_revision"`
	Enabled          bool        `json:"enabled"`
	CadenceMinutes   int64       `json:"cadence_minutes"`
	AuthorLogin      string      `json:"author_login"`
	AuthorID         json.Number `json:"author_id"`
	Repositories     []string    `json:"repositories"`
}

type ScoutClaim struct {
	CaptainID string `json:"captain_id"`
}

type ScoutIssue struct {
	Number      json.Number  `json:"number"`
	AuthorLogin *string      `json:"author_login"`
	AuthorID    *json.Number `json:"author_id"`
	State       string       `json:"state"`
	Title       string       `json:"title"`
	Body        string       `json:"body"`
	UpdatedAt   DateTime     `json:"updated_at"`
}

type ScoutPage struct {
	ClaimID       string       `json:"claim_id"`
	RepositoryID  string       `json:"repository_id"`
	Page          json.Number  `json:"page"`
	VerifiedLogin string       `json:"verified_login"`
	VerifiedID    json.Number  `json:"verified_id"`
	Issues        []ScoutIssue `json:"issues"`
	HasMore       bool         `json:"has_more"`
}

type ScoutFailure struct {
	ClaimID           string       `json:"claim_id"`
	Kind              string       `json:"kind"`
	HTTPStatus        *json.Number `json:"http_status"`
	RetryAfterSeconds json.Number  `json:"retry_after_seconds"`
}

type ScoutDecision struct {
	ExpectedFingerprint string `json:"expected_fingerprint"`
	Decision            string `json:"decision"`
}

func (v *validator) positive(value any, at []any) json.Number {
	number, ok := v.integerType(value, at)
	if ok {
		integer, _ := new(big.Int).SetString(string(number), 10)
		if integer.Sign() <= 0 {
			v.issue("greater_than", "Input should be greater than 0", at, value, map[string]any{"gt": 0})
		}
	}
	return number
}

func (v *validator) scoutCadence(value any, at []any) int64 {
	number, ok := value.(json.Number)
	if ok {
		integer, err := strconv.ParseFloat(string(number), 64)
		if err == nil {
			for _, choice := range []int64{15, 30, 60, 180, 360, 720, 1440} {
				if integer == float64(choice) {
					return choice
				}
			}
		}
	}
	expected := "15, 30, 60, 180, 360, 720 or 1440"
	v.issue("literal_error", "Input should be "+expected, at, value, map[string]any{"expected": expected})
	return 60
}

func ValidateScoutSettings(value any, keys []string) (ScoutSettings, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ScoutSettings{}, modelIssue(value, at)
	}
	v, result := validator{}, ScoutSettings{CadenceMinutes: 60, Repositories: []string{}}
	if value, exists := v.required(body, "expected_revision", at); exists {
		result.ExpectedRevision = v.strictInteger(value, location(at, "expected_revision"), 0)
	}
	if value, exists := body["enabled"]; exists {
		result.Enabled = v.boolean(value, location(at, "enabled"), true)
	}
	if value, exists := body["cadence_minutes"]; exists {
		result.CadenceMinutes = v.scoutCadence(value, location(at, "cadence_minutes"))
	}
	if value, exists := v.required(body, "author_login", at); exists {
		result.AuthorLogin = v.text(value, location(at, "author_login"), false, 0, 0, ScoutLoginPattern)
	}
	if value, exists := v.required(body, "author_id", at); exists {
		result.AuthorID = v.positive(value, location(at, "author_id"))
	}
	if value, exists := v.required(body, "repositories", at); exists {
		where := location(at, "repositories")
		entries, ok := value.([]any)
		if !ok {
			v.issue("list_type", "Input should be a valid list", where, value, nil)
		} else {
			start := len(v.issues)
			for index, entry := range entries {
				result.Repositories = append(result.Repositories, v.text(entry, location(where, index), false, 0, 0, IdentifierPattern))
			}
			if len(v.issues) == start {
				if len(entries) > 100 {
					v.tooLong(value, where, 100, len(entries))
				} else if len(entries) == 0 {
					v.issue("too_short", "List should have at least 1 item after validation, not 0", where, value,
						map[string]any{"field_type": "List", "min_length": 1, "actual_length": 0})
				}
			}
		}
	}
	v.extras(body, keys, []string{"expected_revision", "enabled", "cadence_minutes", "author_login", "author_id", "repositories"}, at)
	if len(v.issues) == 0 {
		seen := map[string]bool{}
		for _, repository := range result.Repositories {
			if seen[repository] {
				v.valueError(body, at, "Scout repositories must be unique")
				break
			}
			seen[repository] = true
		}
	}
	return result, v.issues
}

func ValidateScoutClaim(value any, keys []string) (ScoutClaim, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ScoutClaim{}, modelIssue(value, at)
	}
	v, result := validator{}, ScoutClaim{}
	if value, exists := v.required(body, "captain_id", at); exists {
		result.CaptainID = v.text(value, location(at, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	v.extras(body, keys, []string{"captain_id"}, at)
	return result, v.issues
}

func (v *validator) scoutIssue(value any, at []any) ScoutIssue {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(value, at)...)
		return ScoutIssue{}
	}
	result := ScoutIssue{}
	if value, exists := v.required(body, "number", at); exists {
		result.Number = v.positive(value, location(at, "number"))
	}
	if value, exists := v.required(body, "author_login", at); exists && value != nil {
		text := v.text(value, location(at, "author_login"), false, 0, 0, ScoutAuthorPattern)
		result.AuthorLogin = &text
	}
	if value, exists := v.required(body, "author_id", at); exists && value != nil {
		number := v.positive(value, location(at, "author_id"))
		result.AuthorID = &number
	}
	if value, exists := v.required(body, "state", at); exists {
		result.State = v.choice(value, location(at, "state"), []string{"open", "closed"}, true)
	}
	if value, exists := v.required(body, "title", at); exists {
		result.Title = v.text(value, location(at, "title"), false, 1, 16000, "")
	}
	if value, exists := v.required(body, "body", at); exists {
		result.Body = v.text(value, location(at, "body"), false, 0, 1000000, "")
	}
	if value, exists := v.required(body, "updated_at", at); exists {
		result.UpdatedAt = v.dateTime(value, location(at, "updated_at"))
	}
	v.extras(body, ObjectKeys(value), []string{"number", "author_login", "author_id", "state", "title", "body", "updated_at"}, at)
	return result
}

func ValidateScoutPage(value any, keys []string) (ScoutPage, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ScoutPage{}, modelIssue(value, at)
	}
	v, result := validator{}, ScoutPage{Issues: []ScoutIssue{}}
	if value, exists := v.required(body, "claim_id", at); exists {
		result.ClaimID = v.text(value, location(at, "claim_id"), false, 0, 0, IdentifierPattern)
	}
	if value, exists := v.required(body, "repository_id", at); exists {
		result.RepositoryID = v.text(value, location(at, "repository_id"), false, 0, 0, IdentifierPattern)
	}
	if value, exists := v.required(body, "page", at); exists {
		result.Page = v.positive(value, location(at, "page"))
	}
	if value, exists := v.required(body, "verified_login", at); exists {
		result.VerifiedLogin = v.text(value, location(at, "verified_login"), false, 0, 0, ScoutLoginPattern)
	}
	if value, exists := v.required(body, "verified_id", at); exists {
		result.VerifiedID = v.positive(value, location(at, "verified_id"))
	}
	if value, exists := v.required(body, "issues", at); exists {
		where := location(at, "issues")
		entries, ok := value.([]any)
		if !ok {
			v.issue("list_type", "Input should be a valid list", where, value, nil)
		} else {
			start := len(v.issues)
			for index, entry := range entries {
				result.Issues = append(result.Issues, v.scoutIssue(entry, location(where, index)))
			}
			if len(v.issues) == start && len(entries) > 30 {
				v.tooLong(value, where, 30, len(entries))
			}
		}
	}
	if value, exists := v.required(body, "has_more", at); exists {
		result.HasMore = v.boolean(value, location(at, "has_more"), false)
	}
	v.extras(body, keys, []string{"claim_id", "repository_id", "page", "verified_login", "verified_id", "issues", "has_more"}, at)
	return result, v.issues
}

func ValidateScoutFailure(value any, keys []string) (ScoutFailure, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ScoutFailure{}, modelIssue(value, at)
	}
	v, result := validator{}, ScoutFailure{RetryAfterSeconds: "300"}
	if value, exists := v.required(body, "claim_id", at); exists {
		result.ClaimID = v.text(value, location(at, "claim_id"), false, 0, 0, IdentifierPattern)
	}
	if value, exists := v.required(body, "kind", at); exists {
		result.Kind = v.choice(value, location(at, "kind"), []string{"github", "identity", "credential", "api"}, true)
	}
	if value, exists := body["http_status"]; exists && value != nil {
		number := v.strictInteger(value, location(at, "http_status"), 100)
		result.HTTPStatus = &number
		integer, _ := new(big.Int).SetString(string(number), 10)
		if integer.Cmp(big.NewInt(599)) > 0 {
			v.issue("less_than_equal", "Input should be less than or equal to 599", location(at, "http_status"), value,
				map[string]any{"le": 599})
		}
	}
	if value, exists := body["retry_after_seconds"]; exists {
		result.RetryAfterSeconds = v.strictInteger(value, location(at, "retry_after_seconds"), 0)
		integer, _ := new(big.Int).SetString(string(result.RetryAfterSeconds), 10)
		if integer.Cmp(big.NewInt(604800)) > 0 {
			v.issue("less_than_equal", "Input should be less than or equal to 604800", location(at, "retry_after_seconds"), value,
				map[string]any{"le": 604800})
		}
	}
	v.extras(body, keys, []string{"claim_id", "kind", "http_status", "retry_after_seconds"}, at)
	return result, v.issues
}

func ValidateScoutDecision(value any, keys []string) (ScoutDecision, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ScoutDecision{}, modelIssue(value, at)
	}
	v, result := validator{}, ScoutDecision{}
	if value, exists := v.required(body, "expected_fingerprint", at); exists {
		result.ExpectedFingerprint = v.text(value, location(at, "expected_fingerprint"), false, 0, 0, RevisionPattern)
	}
	if value, exists := v.required(body, "decision", at); exists {
		result.Decision = v.choice(value, location(at, "decision"), []string{"approved", "declined"}, true)
	}
	v.extras(body, keys, []string{"expected_fingerprint", "decision"}, at)
	return result, v.issues
}

func ValidatePathInteger(value, name string) (json.Number, []Issue) {
	v := validator{}
	number, _ := v.queryIntegerValue(value, []any{"path", name})
	return number, v.issues
}
