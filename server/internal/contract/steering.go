package contract

import "encoding/json"

type Steering struct {
	ExpectedVersion json.Number
	SteeredBy       string
	Edits           map[string]any
	Payload         map[string]any
}

type Rerun struct {
	ExpectedVersion json.Number `json:"expected_version"`
	Target          string      `json:"target"`
}

func (v *validator) prerequisites(value any, loc []any) []Prerequisite {
	values, ok := value.([]any)
	if !ok {
		v.issue("Input should be a valid list", loc)
		return nil
	}
	start := len(v.issues)
	result := []Prerequisite{}
	for index, entry := range values {
		at := location(loc, index)
		body, ok := object(entry)
		if !ok {
			v.issues = append(v.issues, modelIssue(at)...)
			continue
		}
		link := Prerequisite{Milestone: "checked_in"}
		if value, ok := v.required(body, "upstream_id", at); ok {
			link.UpstreamID = v.text(value, location(at, "upstream_id"), false, 0, 0, IdentifierPattern)
		}
		if value, ok := body["milestone"]; ok {
			link.Milestone = v.choice(value, location(at, "milestone"), targets)
		}
		v.extras(ObjectKeys(entry), []string{"upstream_id", "milestone"}, at)
		result = append(result, link)
	}
	if len(v.issues) == start && len(values) > 100 {
		v.tooLong(loc, 100, len(values))
	}
	return result
}

func ValidateSteering(value any, keys []string) (Steering, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Steering{}, modelIssue(prefix)
	}
	v := validator{}
	result := Steering{SteeredBy: "owner", Edits: map[string]any{}, Payload: map[string]any{}}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
		result.Payload["expected_version"] = result.ExpectedVersion
	}
	for _, field := range []string{"goal", "context_links", "acceptance_criteria", "priority", "label", "dependencies", "target"} {
		value, exists := body[field]
		if !exists {
			continue
		}
		var normalized any
		if value != nil {
			switch field {
			case "goal":
				normalized = v.text(value, location(prefix, field), true, 1, 16000, "")
			case "context_links", "acceptance_criteria":
				normalized = v.texts(value, location(prefix, field), 50)
			case "priority":
				normalized = v.choice(value, location(prefix, field), []string{"low", "normal", "urgent"})
			case "label":
				normalized = v.text(value, location(prefix, field), false, 0, 0, labelPattern)
			case "dependencies":
				normalized = v.prerequisites(value, location(prefix, field))
			case "target":
				normalized = v.choice(value, location(prefix, field), targets)
			}
		}
		result.Edits[field], result.Payload[field] = normalized, normalized
	}
	if value, exists := body["steered_by"]; exists {
		result.SteeredBy = v.choice(value, location(prefix, "steered_by"), []string{"owner", "captain"})
		result.Payload["steered_by"] = result.SteeredBy
	}
	v.extras(keys, []string{"expected_version", "goal", "context_links", "acceptance_criteria", "priority", "label",
		"dependencies", "target", "steered_by"}, prefix)
	return result, v.issues
}

func ValidateRerun(value any, keys []string) (Rerun, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Rerun{}, modelIssue(prefix)
	}
	v, result := validator{}, Rerun{}
	if value, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(value, location(prefix, "expected_version"), 1)
	}
	if value, ok := v.required(body, "target", prefix); ok {
		result.Target = v.choice(value, location(prefix, "target"), targets)
	}
	v.extras(keys, []string{"expected_version", "target"}, prefix)
	return result, v.issues
}
