package contract

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
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

func (v *validator) queryIntegerValue(value string, loc []any) (json.Number, bool) {
	integer, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		v.issue("Input must be a decimal integer", loc)
		return "0", false
	}
	return json.Number(strconv.FormatInt(integer, 10)), true
}

func (v *validator) queryLimit(value string, loc []any, minimum, maximum int64) int {
	number, ok := v.queryIntegerValue(value, loc)
	if !ok {
		return 0
	}
	integer, _ := number.Int64()
	if integer < minimum {
		v.issue(fmt.Sprintf("Input should be greater than or equal to %d", minimum), loc)
		return 0
	}
	if integer > maximum {
		v.issue(fmt.Sprintf("Input should be less than or equal to %d", maximum), loc)
		return 0
	}
	return int(integer)
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
