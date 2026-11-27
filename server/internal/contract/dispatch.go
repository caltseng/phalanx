package contract

import (
	"encoding/json"
	"math/big"
)

type DispatchUpdate struct {
	Paused             *bool        `json:"paused,omitempty"`
	MaxWriters         *json.Number `json:"max_writers,omitempty"`
	MaxBuildingWriters *json.Number `json:"max_building_writers,omitempty"`
}

func (v *validator) dispatchLimit(value any, loc []any) *json.Number {
	number, ok := v.integerType(value, loc)
	if !ok {
		return nil
	}
	integer, _ := new(big.Int).SetString(string(number), 10)
	if integer.Sign() <= 0 {
		v.issue("greater_than", "Input should be greater than 0", loc, value, map[string]any{"gt": 0})
	} else if integer.Cmp(big.NewInt(2147483647)) > 0 {
		v.issue("less_than_equal", "Input should be less than or equal to 2147483647", loc, value, map[string]any{"le": 2147483647})
	}
	return &number
}

func ValidateDispatchUpdate(value any, keys []string) (DispatchUpdate, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return DispatchUpdate{}, modelIssue(value, prefix)
	}
	v, result := validator{}, DispatchUpdate{}
	for _, value := range body {
		if value == nil {
			v.issue("value_error", "Value error, Dispatch settings cannot be null; omit unchanged fields",
				prefix, body, map[string]any{"error": map[string]any{}})
			return result, v.issues
		}
	}
	if value, exists := body["paused"]; exists {
		paused := v.boolean(value, location(prefix, "paused"), true)
		result.Paused = &paused
	}
	if value, exists := body["max_writers"]; exists {
		result.MaxWriters = v.dispatchLimit(value, location(prefix, "max_writers"))
	}
	if value, exists := body["max_building_writers"]; exists {
		result.MaxBuildingWriters = v.dispatchLimit(value, location(prefix, "max_building_writers"))
	}
	v.extras(body, keys, []string{"paused", "max_writers", "max_building_writers"}, prefix)
	return result, v.issues
}
