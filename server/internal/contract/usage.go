package contract

import (
	"encoding/json"
	"errors"
	"math/big"
	"time"
)

const MaxUsageCounter = 9007199254740991
const UsageAmountPattern = `^(0|[1-9][0-9]{0,14})(\.[0-9]{1,9})?$`

var TokenFields = []string{"input", "output", "cache_read", "cache_write", "reasoning", "total"}

// SumUsageCounters retains exact subtotals, using text above the JSON-safe bound.
func SumUsageCounters(counters []json.Number) (any, error) {
	sum, err := SumNumbers(counters)
	if sum == nil || err != nil {
		return sum, err
	}
	total, ok := sum.(json.Number)
	if !ok {
		return nil, errors.New("Included usage counter is not an integer")
	}
	if value, err := total.Int64(); value > MaxUsageCounter || err != nil && total[0] != '-' {
		return string(total), nil
	}
	return total, nil
}

type Tokens struct {
	Input      *json.Number `json:"input"`
	Output     *json.Number `json:"output"`
	CacheRead  *json.Number `json:"cache_read"`
	CacheWrite *json.Number `json:"cache_write"`
	Reasoning  *json.Number `json:"reasoning"`
	Total      *json.Number `json:"total"`
}

func (tokens Tokens) Fields() map[string]*json.Number {
	return map[string]*json.Number{"input": tokens.Input, "output": tokens.Output, "cache_read": tokens.CacheRead,
		"cache_write": tokens.CacheWrite, "reasoning": tokens.Reasoning, "total": tokens.Total}
}

type Quantity struct {
	Amount string `json:"amount"`
	Unit   string `json:"unit"`
	Basis  string `json:"basis"`
	Source string `json:"source"`
}

type UsageSnapshot struct {
	Runtime       string      `json:"runtime"`
	Provider      string      `json:"provider"`
	Account       string      `json:"account"`
	Origin        string      `json:"origin"`
	SessionID     string      `json:"session_id"`
	Source        string      `json:"source"`
	SourceVersion string      `json:"source_version"`
	Epoch         string      `json:"epoch"`
	Model         *string     `json:"model"`
	Sequence      json.Number `json:"sequence"`
	WindowStart   Timestamp   `json:"window_start"`
	Cutoff        Timestamp   `json:"cutoff"`
	ObservedAt    Timestamp   `json:"observed_at"`
	Scope         string      `json:"scope"`
	Status        string      `json:"status"`
	Reason        string      `json:"reason"`
	Tokens        Tokens      `json:"tokens"`
	Credits       *Quantity   `json:"credits"`
	Money         *Quantity   `json:"money"`
}

type CaptainUsage struct {
	CaptainID string        `json:"captain_id"`
	Snapshot  UsageSnapshot `json:"snapshot"`
}

func (v *validator) usageCounter(value any, at []any) json.Number {
	number := v.strictInteger(value, at, 0)
	integer, _ := new(big.Int).SetString(string(number), 10)
	if integer.Cmp(big.NewInt(MaxUsageCounter)) > 0 {
		v.issue("Input should be less than or equal to 9007199254740991", at)
	}
	return number
}

func (v *validator) tokens(value any, at []any) Tokens {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return Tokens{}
	}
	result := Tokens{}
	fields := []**json.Number{&result.Input, &result.Output, &result.CacheRead, &result.CacheWrite, &result.Reasoning, &result.Total}
	for index, name := range TokenFields {
		if value, exists := body[name]; exists && value != nil {
			number := v.usageCounter(value, location(at, name))
			*fields[index] = &number
		}
	}
	v.extras(ObjectKeys(value), TokenFields, at)
	return result
}

func (v *validator) quantity(value any, at []any) *Quantity {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return nil
	}
	result := &Quantity{}
	if value, exists := v.required(body, "amount", at); exists {
		result.Amount = v.text(value, location(at, "amount"), false, 0, 0, UsageAmountPattern)
	}
	if value, exists := v.required(body, "unit", at); exists {
		result.Unit = v.text(value, location(at, "unit"), true, 1, 200, "")
	}
	if value, exists := v.required(body, "basis", at); exists {
		result.Basis = v.choice(value, location(at, "basis"), []string{"provider_reported", "estimated"})
	}
	if value, exists := v.required(body, "source", at); exists {
		result.Source = v.text(value, location(at, "source"), true, 1, 200, "")
	}
	v.extras(ObjectKeys(value), []string{"amount", "unit", "basis", "source"}, at)
	return result
}

func (v *validator) usageSnapshot(value any, at []any, keys []string, now time.Time) UsageSnapshot {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return UsageSnapshot{}
	}
	start := len(v.issues)
	result := UsageSnapshot{}
	names := []string{"runtime", "provider", "account", "origin", "session_id", "source", "source_version", "epoch"}
	fields := []*string{&result.Runtime, &result.Provider, &result.Account, &result.Origin, &result.SessionID,
		&result.Source, &result.SourceVersion, &result.Epoch}
	for index, name := range names {
		if value, exists := v.required(body, name, at); exists {
			*fields[index] = v.text(value, location(at, name), true, 1, 200, "")
		}
	}
	if value, exists := body["model"]; exists && value != nil {
		text := v.text(value, location(at, "model"), true, 1, 200, "")
		result.Model = &text
	}
	if value, exists := v.required(body, "sequence", at); exists {
		result.Sequence = v.usageCounter(value, location(at, "sequence"))
	}
	dates := []*Timestamp{&result.WindowStart, &result.Cutoff, &result.ObservedAt}
	for index, name := range []string{"window_start", "cutoff", "observed_at"} {
		if value, exists := v.required(body, name, at); exists {
			*dates[index] = v.timestamp(value, location(at, name))
		}
	}
	if value, exists := v.required(body, "scope", at); exists {
		result.Scope = v.choice(value, location(at, "scope"), []string{"item_exclusive", "session_lifetime"})
	}
	if value, exists := v.required(body, "status", at); exists {
		result.Status = v.choice(value, location(at, "status"), []string{"observed", "partial", "unavailable", "pending_final"})
	}
	if value, exists := v.required(body, "reason", at); exists {
		result.Reason = v.text(value, location(at, "reason"), true, 1, 1000, "")
	}
	if value, exists := body["tokens"]; exists {
		result.Tokens = v.tokens(value, location(at, "tokens"))
	}
	if value, exists := body["credits"]; exists && value != nil {
		result.Credits = v.quantity(value, location(at, "credits"))
	}
	if value, exists := body["money"]; exists && value != nil {
		result.Money = v.quantity(value, location(at, "money"))
	}
	v.extras(keys, []string{"runtime", "provider", "account", "origin", "session_id", "source", "source_version",
		"epoch", "model", "sequence", "window_start", "cutoff", "observed_at", "scope", "status", "reason",
		"tokens", "credits", "money"}, at)
	if len(v.issues) == start {
		known := result.Credits != nil || result.Money != nil
		for _, number := range result.Tokens.Fields() {
			known = known || number != nil
		}
		message := ""
		switch {
		case result.WindowStart.After(result.Cutoff.Time) || result.Cutoff.After(result.ObservedAt.Time):
			message = "Usage window must precede observation"
		case result.ObservedAt.After(now):
			message = "Usage observation cannot be in the future"
		case result.Status == "unavailable" && known:
			message = "Unavailable acquisition must not invent or erase numeric counters"
		case result.Status != "unavailable" && !known:
			message = "Numeric usage status requires a supplied measurement; otherwise use unavailable"
		}
		if message != "" {
			v.valueError(at, message)
		}
	}
	return result
}

func ValidateUsageSnapshot(value any, keys []string, now time.Time) (UsageSnapshot, []Issue) {
	v := validator{}
	result := v.usageSnapshot(value, []any{"body"}, keys, now)
	return result, v.issues
}

func ValidateCaptainUsage(value any, keys []string, now time.Time) (CaptainUsage, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return CaptainUsage{}, modelIssue(at)
	}
	v, result := validator{}, CaptainUsage{}
	if value, exists := v.required(body, "captain_id", at); exists {
		result.CaptainID = v.text(value, location(at, "captain_id"), false, 0, 0, IdentifierPattern)
	}
	if value, exists := v.required(body, "snapshot", at); exists {
		result.Snapshot = v.usageSnapshot(value, location(at, "snapshot"), ObjectKeys(value), now)
	}
	v.extras(keys, []string{"captain_id", "snapshot"}, at)
	return result, v.issues
}
