package contract

import (
	"fmt"
	"math/big"
	"net/url"
	"regexp"
	"strings"
)

type InboxQuery struct {
	Limit     int
	AfterTime string
	AfterID   string
}

func ValidateLimit(values url.Values, fallback int) (int, []Issue) {
	v := validator{}
	if value, exists := lastQuery(values, "limit"); exists {
		fallback = v.queryLimit(value, []any{"query", "limit"}, 1, 100)
	}
	return fallback, v.issues
}

func lastQuery(values url.Values, name string) (string, bool) {
	entries, present := values[name]
	if !present || len(entries) == 0 {
		return "", false
	}
	return entries[len(entries)-1], true
}

var queryInteger = regexp.MustCompile(`^[+-]?[0-9](_?[0-9])*(\.0+)?$`)

func (v *validator) queryLimit(value string, loc []any, minimum, maximum int64) int {
	text := strings.TrimFunc(value, pythonSpace)
	if !queryInteger.MatchString(text) {
		v.issue("int_parsing", "Input should be a valid integer, unable to parse string as an integer", loc, value, nil)
		return 0
	}
	integerPart := strings.SplitN(text, ".", 2)[0]
	integerPart = strings.ReplaceAll(integerPart, "_", "")
	digits := strings.TrimLeft(strings.TrimLeft(integerPart, "+-"), "0")
	if len(digits) > 4300 {
		v.issue("int_parsing_size", "Unable to parse input string as an integer, exceeded maximum size", loc, value, nil)
		return 0
	}
	integer, _ := new(big.Int).SetString(integerPart, 10)
	if integer.Cmp(big.NewInt(minimum)) < 0 {
		v.issue("greater_than_equal", fmt.Sprintf("Input should be greater than or equal to %d", minimum), loc, value, map[string]any{"ge": minimum})
		return 0
	}
	if integer.Cmp(big.NewInt(maximum)) > 0 {
		v.issue("less_than_equal", fmt.Sprintf("Input should be less than or equal to %d", maximum), loc, value, map[string]any{"le": maximum})
		return 0
	}
	return int(integer.Int64())
}

func ValidateInboxQuery(values url.Values) (InboxQuery, []Issue) {
	v := validator{}
	result := InboxQuery{Limit: 50}
	if value, present := lastQuery(values, "limit"); present {
		result.Limit = v.queryLimit(value, []any{"query", "limit"}, 1, 100)
	}
	if value, present := lastQuery(values, "after_time"); present {
		result.AfterTime = v.text(value, []any{"query", "after_time"}, false, 0, 40, "")
	}
	if value, present := lastQuery(values, "after_id"); present {
		result.AfterID = v.text(value, []any{"query", "after_id"}, false, 0, 0, IdentifierPattern)
	}
	return result, v.issues
}
