package contract

import (
	"encoding/json"
	"math/big"
	"regexp"
	"strconv"
)

const RevisionPattern = `^[a-f0-9]{64}$`

type DisplayTitle struct {
	ExpectedRevision json.Number `json:"expected_revision"`
	Title            *string     `json:"title"`
}

var integerToken = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)

func (v *validator) integerType(value any, loc []any) (json.Number, bool) {
	number, ok := value.(json.Number)
	if !ok || !integerToken.MatchString(string(number)) {
		v.issue("Input should be a valid integer", loc)
		return "0", false
	}
	result, _ := new(big.Int).SetString(string(number), 10)
	return json.Number(result.String()), true
}

func (v *validator) strictInteger(value any, loc []any, minimum int64) json.Number {
	number, ok := v.integerType(value, loc)
	if !ok {
		return number
	}
	result, _ := new(big.Int).SetString(string(number), 10)
	if result.Cmp(big.NewInt(minimum)) < 0 {
		v.issue("Input should be greater than or equal to "+strconv.FormatInt(minimum, 10),
			loc)
	}
	return json.Number(result.String())
}

func ValidateDisplayTitle(value any, keys []string) (DisplayTitle, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return DisplayTitle{}, modelIssue(prefix)
	}
	v := validator{}
	result := DisplayTitle{}
	if x, ok := v.required(body, "expected_revision", prefix); ok {
		result.ExpectedRevision = v.strictInteger(x, location(prefix, "expected_revision"), 0)
	}
	if x, ok := v.required(body, "title", prefix); ok && x != nil {
		text := v.text(x, location(prefix, "title"), true, 1, 200, `^[^\r\n]+$`)
		result.Title = &text
	}
	v.extras(keys, []string{"expected_revision", "title"}, prefix)
	return result, v.issues
}

func ValidateRevision(value string) []Issue {
	v := validator{}
	v.text(value, []any{"path", "revision"}, false, 0, 0, RevisionPattern)
	return v.issues
}
