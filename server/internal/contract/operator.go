package contract

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net/url"
	"regexp"
	"strings"
)

const LabelPattern = `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`

type OperatorQuery struct {
	Kind                                             string
	Limit                                            int
	Cursor                                           *string
	Repository, Label, Status, Stage, Outcome, Query *string
	ClosedAfter                                      *DateTime
}

func operatorPageQuery(v *validator, values url.Values, result *OperatorQuery) {
	if value, exists := lastQuery(values, "limit"); exists {
		result.Limit = v.queryLimit(value, []any{"query", "limit"}, 1, 100)
	}
	if value, exists := lastQuery(values, "cursor"); exists {
		text := v.text(value, []any{"query", "cursor"}, false, 0, 512, "")
		result.Cursor = &text
	}
}

func ValidateOperatorPage(values url.Values) (OperatorQuery, []Issue) {
	v, result := validator{}, OperatorQuery{Limit: 25}
	operatorPageQuery(&v, values, &result)
	return result, v.issues
}

func ValidateOperatorQuery(values url.Values) (OperatorQuery, []Issue) {
	v, result := validator{}, OperatorQuery{Kind: "open", Limit: 25}
	if value, exists := lastQuery(values, "kind"); exists {
		result.Kind = v.choice(value, []any{"query", "kind"}, []string{"open", "closed"}, true)
	}
	operatorPageQuery(&v, values, &result)
	if value, exists := lastQuery(values, "repository"); exists {
		text := v.text(value, []any{"query", "repository"}, false, 0, 0, IdentifierPattern)
		result.Repository = &text
	}
	if value, exists := lastQuery(values, "label"); exists {
		text := v.text(value, []any{"query", "label"}, false, 0, 0, LabelPattern)
		result.Label = &text
	}
	if value, exists := lastQuery(values, "status"); exists {
		text := v.choice(value, []any{"query", "status"}, []string{"queued", "running", "waiting_for_input",
			"waiting_for_external", "completed", "failed", "cancelled"}, false)
		result.Status = &text
	}
	if value, exists := lastQuery(values, "stage"); exists {
		text := v.choice(value, []any{"query", "stage"}, stages, false)
		result.Stage = &text
	}
	if value, exists := lastQuery(values, "outcome"); exists {
		text := v.choice(value, []any{"query", "outcome"}, []string{"report_stored", "draft_published", "ready_for_check_in",
			"checked_in", "failed", "cancelled"}, true)
		result.Outcome = &text
	}
	if value, exists := lastQuery(values, "q"); exists {
		text := v.text(value, []any{"query", "q"}, false, 1, 200, "")
		result.Query = &text
	}
	if value, exists := lastQuery(values, "closed_after"); exists {
		moment := v.dateTime(value, []any{"query", "closed_after"})
		result.ClosedAfter = &moment
	}
	return result, v.issues
}

func ValidateLabel(value, locationType string) []Issue {
	v := validator{}
	v.text(value, []any{locationType, "label"}, false, 0, 0, LabelPattern)
	return v.issues
}

func ValidateBoardIdentifier(value, name string) []Issue {
	v := validator{}
	v.text(value, []any{"path", name}, false, 0, 0, `^[0-9a-f]{32}$`)
	return v.issues
}

func EncodeCursor(scope string, key []any) (string, error) {
	data, err := json.Marshal([]any{1, scope, key})
	if err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(data), nil
}

var cursorStamp = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$`)
var cursorID = regexp.MustCompile(`^[0-9a-f]{32}$`)

func DecodeCursor(cursor *string, scope string, shape []string) ([]any, error) {
	if cursor == nil {
		return nil, nil
	}
	invalid := errors.New("Invalid cursor")
	bytes, err := base64.RawURLEncoding.Strict().DecodeString(*cursor)
	if err != nil || base64.RawURLEncoding.EncodeToString(bytes) != *cursor {
		return nil, invalid
	}
	value, _, err := Decode(bytes)
	if err != nil {
		return nil, invalid
	}
	outer, ok := value.([]any)
	if !ok || len(outer) != 3 {
		return nil, invalid
	}
	version, ok := outer[0].(json.Number)
	if !ok || version != "1" {
		return nil, invalid
	}
	actualScope, ok := outer[1].(string)
	if !ok || actualScope != scope {
		return nil, invalid
	}
	key, ok := outer[2].([]any)
	if !ok || len(key) != len(shape) {
		return nil, invalid
	}
	for index, field := range shape {
		switch field {
		case "stamp":
			text, ok := key[index].(string)
			if !ok || !cursorStamp.MatchString(text) {
				return nil, invalid
			}
		case "id":
			text, ok := key[index].(string)
			if !ok || !cursorID.MatchString(text) {
				return nil, invalid
			}
		case "rank", "version":
			number, ok := key[index].(json.Number)
			if !ok || strings.ContainsAny(string(number), ".eE") {
				return nil, invalid
			}
			integer, ok := new(big.Int).SetString(string(number), 10)
			if !ok || field == "rank" && (integer.Sign() < 0 || integer.Cmp(big.NewInt(1)) > 0) ||
				field == "version" && (integer.Sign() <= 0 || !integer.IsInt64()) {
				return nil, invalid
			}
		default:
			return nil, invalid
		}
	}
	return key, nil
}
