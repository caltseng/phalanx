// Package contract owns request validation and historical receipt canonicalization.
package contract

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

const IdentifierPattern = `^[A-Za-z0-9_-]{1,100}$`
const labelPattern = `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`

var identifier = regexp.MustCompile(IdentifierPattern)
var label = regexp.MustCompile(labelPattern)

// Keys retain the public patterns used in validation messages.
var textPatterns = map[string]*regexp.Regexp{
	IdentifierPattern:  identifier,
	labelPattern:       label,
	FlagKindPattern:    regexp.MustCompile(FlagKindPattern),
	FlagSubjectPattern: regexp.MustCompile(FlagSubjectPattern),
	NoncePattern:       regexp.MustCompile(NoncePattern),
	URLPattern:         regexp.MustCompile(validationPatternSource(URLPattern)),
	SHAPattern:         regexp.MustCompile(SHAPattern),
	RevisionPattern:    regexp.MustCompile(RevisionPattern),
	PullURLPattern:     regexp.MustCompile(PullURLPattern),
	ScoutLoginPattern:  regexp.MustCompile(ScoutLoginPattern),
	ScoutAuthorPattern: regexp.MustCompile(ScoutAuthorPattern),
	UsageAmountPattern: regexp.MustCompile(UsageAmountPattern),
	`^[0-9a-f]{32}$`:   cursorID,
	`\S`:               regexp.MustCompile(validationPatternSource(`\S`)),
	`^[^\r\n]+$`:       regexp.MustCompile(`^[^\r\n]+$`),
}

type Prerequisite struct {
	UpstreamID string `json:"upstream_id"`
	Milestone  string `json:"milestone"`
}

type Intake struct {
	RepositoryID       string         `json:"repository_id"`
	Goal               string         `json:"goal"`
	Target             string         `json:"target"`
	ContextLinks       []string       `json:"context_links"`
	AcceptanceCriteria []string       `json:"acceptance_criteria"`
	TargetBranch       *string        `json:"target_branch"`
	Priority           string         `json:"priority"`
	Label              *string        `json:"label"`
	Dependencies       []Prerequisite `json:"dependencies"`
	PublicationMode    string         `json:"publication_mode"`
}

type Registration struct {
	ID     string `json:"id"`
	Remote string `json:"remote"`
}

type Issue struct {
	Loc []any  `json:"loc"`
	Msg string `json:"msg"`
}

type validator struct {
	issues []Issue
}

func (v *validator) issue(message string, loc []any) {
	v.issues = append(v.issues, Issue{Loc: loc, Msg: message})
}

func location(prefix []any, field any) []any {
	return append(append([]any{}, prefix...), field)
}

func (v *validator) required(body map[string]any, field string, prefix []any) (any, bool) {
	value, ok := body[field]
	if !ok {
		v.issue("Field required", location(prefix, field))
	}
	return value, ok
}

func (v *validator) text(value any, loc []any, strip bool, min, max int, pattern string) string {
	text, ok := value.(string)
	if !ok {
		v.issue("Input should be a valid string", loc)
		return ""
	}
	if strip {
		text = strings.TrimSpace(text)
	}
	size := utf8.RuneCountInString(text)
	if min > 0 && size < min {
		v.issue(fmt.Sprintf("String should have at least %d character", min), loc)
	} else if max > 0 && size > max {
		v.issue(fmt.Sprintf("String should have at most %d characters", max), loc)
	} else if pattern != "" && !validationPattern(pattern).MatchString(text) {
		v.issue(fmt.Sprintf("String should match pattern '%s'", pattern), loc)
	}
	return text
}

func validationPattern(pattern string) *regexp.Regexp {
	compiled, ok := textPatterns[pattern]
	if !ok {
		panic(fmt.Sprintf("unregistered validation pattern %q", pattern))
	}
	return compiled
}

func validationPatternSource(pattern string) string {
	// Rust/Pydantic's \S uses Unicode White_Space; Go's Perl class is ASCII.
	const atom = `[^\x09-\x0d\x20\x85\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]`
	pattern = strings.ReplaceAll(pattern, `\S`, atom)
	// Go RE2 caps individual repetition bounds at 1000.
	pattern = strings.ReplaceAll(pattern, atom+"{1,2000}", atom+"{1,1000}"+atom+"{0,1000}")
	return pattern
}

func (v *validator) choice(value any, loc []any, choices []string) string {
	text, ok := value.(string)
	if ok {
		for _, choice := range choices {
			if choice == text {
				return text
			}
		}
	}
	quoted := make([]string, len(choices))
	for i, choice := range choices {
		quoted[i] = "'" + choice + "'"
	}
	expected := strings.Join(quoted[:len(quoted)-1], ", ") + " or " + quoted[len(quoted)-1]
	v.issue("Input should be "+expected, loc)
	return ""
}

func (v *validator) texts(value any, loc []any, limit int) []string {
	values, ok := value.([]any)
	if !ok {
		v.issue("Input should be a valid list", loc)
		return nil
	}
	start := len(v.issues)
	result := make([]string, len(values))
	for i, entry := range values {
		result[i] = v.text(entry, location(loc, i), true, 1, 16000, "")
	}
	// Pydantic validates elements before checking the collection bound.
	if len(v.issues) == start && len(values) > limit {
		v.tooLong(loc, limit, len(values))
	}
	return result
}

func (v *validator) tooLong(loc []any, limit, length int) {
	v.issue(fmt.Sprintf("List should have at most %d items after validation, not %d", limit, length), loc)
}

func (v *validator) extras(keys, allowed []string, prefix []any) {
	for _, key := range keys {
		found := false
		for _, field := range allowed {
			if key == field {
				found = true
				break
			}
		}
		if !found {
			v.issue("Extra inputs are not permitted", location(prefix, key))
		}
	}
}

func object(value any) (map[string]any, bool) {
	body, ok := value.(map[string]any)
	return body, ok
}

func ObjectValues(value any) (map[string]any, bool) {
	return object(value)
}

func modelIssue(prefix []any) []Issue {
	return []Issue{{Loc: prefix, Msg: "Input should be a valid object"}}
}

var targets = []string{"report", "ready_for_check_in", "checked_in"}

func ValidateIntake(value any, keys []string) (Intake, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Intake{}, modelIssue(prefix)
	}
	v := validator{}
	result := Intake{Target: "checked_in", ContextLinks: []string{}, AcceptanceCriteria: []string{}, Dependencies: []Prerequisite{}, Priority: "normal", PublicationMode: "ready"}
	if x, ok := v.required(body, "repository_id", prefix); ok {
		result.RepositoryID = v.text(x, location(prefix, "repository_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "goal", prefix); ok {
		result.Goal = v.text(x, location(prefix, "goal"), true, 1, 16000, "")
	}
	if x, ok := body["target"]; ok {
		result.Target = v.choice(x, location(prefix, "target"), targets)
	}
	if x, ok := body["context_links"]; ok {
		result.ContextLinks = v.texts(x, location(prefix, "context_links"), 50)
	}
	if x, ok := body["acceptance_criteria"]; ok {
		result.AcceptanceCriteria = v.texts(x, location(prefix, "acceptance_criteria"), 50)
	}
	if x, ok := body["target_branch"]; ok && x != nil {
		text := v.text(x, location(prefix, "target_branch"), true, 1, 16000, "")
		result.TargetBranch = &text
	}
	if x, ok := body["priority"]; ok {
		result.Priority = v.choice(x, location(prefix, "priority"), []string{"low", "normal", "urgent"})
	}
	if x, ok := body["label"]; ok && x != nil {
		text := v.text(x, location(prefix, "label"), false, 0, 0, labelPattern)
		result.Label = &text
	}
	if x, ok := body["dependencies"]; ok {
		loc := location(prefix, "dependencies")
		values, ok := x.([]any)
		if !ok {
			v.issue("Input should be a valid list", loc)
		} else {
			start := len(v.issues)
			for i, entry := range values {
				at := location(loc, i)
				dependency, ok := object(entry)
				if !ok {
					v.issues = append(v.issues, modelIssue(at)...)
					continue
				}
				link := Prerequisite{Milestone: "checked_in"}
				if y, ok := v.required(dependency, "upstream_id", at); ok {
					link.UpstreamID = v.text(y, location(at, "upstream_id"), false, 0, 0, IdentifierPattern)
				}
				if y, ok := dependency["milestone"]; ok {
					link.Milestone = v.choice(y, location(at, "milestone"), targets)
				}
				v.extras(ObjectKeys(entry), []string{"upstream_id", "milestone"}, at)
				result.Dependencies = append(result.Dependencies, link)
			}
			if len(v.issues) == start && len(values) > 100 {
				v.tooLong(loc, 100, len(values))
			}
		}
	}
	if x, ok := body["publication_mode"]; ok {
		result.PublicationMode = v.choice(x, location(prefix, "publication_mode"), []string{"ready", "draft"})
	}
	v.extras(keys, []string{"repository_id", "goal", "target", "context_links", "acceptance_criteria", "target_branch", "priority", "label", "dependencies", "publication_mode"}, prefix)
	if len(v.issues) == 0 && result.Target == "report" && result.PublicationMode == "draft" {
		v.issue("Value error, Draft publication requires a PR target", prefix)
	}
	return result, v.issues
}

func ValidateRegistration(value any, keys []string) (Registration, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return Registration{}, modelIssue(prefix)
	}
	v := validator{}
	result := Registration{}
	if x, ok := v.required(body, "id", prefix); ok {
		result.ID = v.text(x, location(prefix, "id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "remote", prefix); ok {
		result.Remote = v.text(x, location(prefix, "remote"), true, 1, 16000, "")
	}
	v.extras(keys, []string{"id", "remote"}, prefix)
	return result, v.issues
}

func ValidateIdentifier(text string, name string) []Issue {
	if identifier.MatchString(text) {
		return nil
	}
	return []Issue{{Loc: []any{"path", name}, Msg: "String should match pattern '" + IdentifierPattern + "'"}}
}

func ValidateKey(key string) []Issue {
	v := validator{}
	v.text(key, []any{"header", "Idempotency-Key"}, false, 1, 200, "")
	return v.issues
}

// Material includes all validated defaults and explicit nulls. Only intake's
// internal compatibility fingerprint adds the removed field.
func (request Intake) Material() (map[string]any, error) {
	data, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	var value map[string]any
	err = json.Unmarshal(data, &value)
	return value, err
}
