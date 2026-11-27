package contract

const FlagKindPattern = `^[a-z][a-z0-9_]{0,63}$`
const FlagSubjectPattern = `^[A-Za-z0-9_-]{0,100}$`

type FlagKey struct {
	Kind        string `json:"kind"`
	ItemID      string `json:"item_id"`
	ExecutionID string `json:"execution_id"`
	CaptainID   string `json:"captain_id"`
}

type Flag struct {
	FlagKey
	Reason      string `json:"reason"`
	Recovery    string `json:"recovery"`
	NotifyOwner bool   `json:"notify_owner"`
}

func (v *validator) flagKey(body map[string]any, at []any) FlagKey {
	result := FlagKey{}
	if value, exists := v.required(body, "kind", at); exists {
		result.Kind = v.text(value, location(at, "kind"), false, 0, 0, FlagKindPattern)
	}
	names := []string{"item_id", "execution_id", "captain_id"}
	fields := []*string{&result.ItemID, &result.ExecutionID, &result.CaptainID}
	for index, name := range names {
		if value, exists := body[name]; exists {
			*fields[index] = v.text(value, location(at, name), false, 0, 0, FlagSubjectPattern)
		}
	}
	return result
}

func ValidateFlagKey(value any, keys []string) (FlagKey, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return FlagKey{}, modelIssue(value, at)
	}
	v := validator{}
	result := v.flagKey(body, at)
	v.extras(body, keys, []string{"kind", "item_id", "execution_id", "captain_id"}, at)
	return result, v.issues
}

func ValidateFlag(value any, keys []string) (Flag, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return Flag{}, modelIssue(value, at)
	}
	v := validator{}
	result := Flag{FlagKey: v.flagKey(body, at)}
	if value, exists := v.required(body, "reason", at); exists {
		result.Reason = v.text(value, location(at, "reason"), true, 1, 16000, "")
	}
	if value, exists := v.required(body, "recovery", at); exists {
		result.Recovery = v.text(value, location(at, "recovery"), true, 1, 16000, "")
	}
	if value, exists := body["notify_owner"]; exists {
		result.NotifyOwner = v.boolean(value, location(at, "notify_owner"), false)
	}
	v.extras(body, keys, []string{"kind", "item_id", "execution_id", "captain_id", "reason", "recovery", "notify_owner"}, at)
	return result, v.issues
}
