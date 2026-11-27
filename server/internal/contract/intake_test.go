package contract

import (
	"encoding/json"
	"testing"
)

func TestTextPatternValidationDoesNotAllocate(t *testing.T) {
	for _, test := range []struct {
		pattern string
		value   string
	}{
		{IdentifierPattern, "repository"},
		{URLPattern, "runtime://native-session"},
		{`\S`, "\u001c"},
	} {
		t.Run(test.pattern, func(t *testing.T) {
			v := validator{}
			loc := []any{"body", "value"}
			value := any(test.value)
			allocations := testing.AllocsPerRun(100, func() {
				v.text(value, loc, false, 0, 0, test.pattern)
			})
			if len(v.issues) != 0 {
				t.Fatal(v.issues)
			}
			if allocations != 0 {
				t.Fatalf("pattern validation allocated %g times per field", allocations)
			}
		})
	}
}

func TestCanonicalUnicodeDefaultsPresenceAndRetiredField(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":" \u96ea \ud83d\ude80 \u001c "}`))
	if err != nil {
		t.Fatal(err)
	}
	request, issues := ValidateIntake(value, keys)
	if len(issues) != 0 || request.Goal != "雪 🚀 \x1c" {
		t.Fatal(request, issues)
	}
	material, err := request.Material()
	if err != nil {
		t.Fatal(err)
	}
	material["runner_affinity"] = nil
	text, err := Canonical(material)
	expected := `{"acceptance_criteria":[],"context_links":[],"dependencies":[],"goal":"\u96ea \ud83d\ude80 \u001c","label":null,"priority":"normal","publication_mode":"ready","repository_id":"repo","runner_affinity":null,"target":"checked_in","target_branch":null}`
	if err != nil || text != expected {
		t.Fatal(text, err)
	}
	if _, issues := ValidateIntake(material, ObjectKeys(material)); len(issues) != 1 || issues[0].Type != "extra_forbidden" {
		t.Fatal("internal null leaked into public contract", issues)
	}
}

func TestNoNumericCoercionOrPrecisionLoss(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":9007199254740993}`))
	if err != nil {
		t.Fatal(err)
	}
	_, issues := ValidateIntake(value, keys)
	if len(issues) != 1 || issues[0].Type != "string_type" || issues[0].Input != json.Number("9007199254740993") {
		t.Fatal(issues)
	}
}

func TestDuplicateJSONLastWinsAndExtraOrder(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":"old","z":1,"a":2,"goal":"new"}`))
	if err != nil {
		t.Fatal(err)
	}
	request, issues := ValidateIntake(value, keys)
	if request.Goal != "new" || len(issues) != 2 || issues[0].Loc[1] != "z" || issues[1].Loc[1] != "a" {
		t.Fatal(request, issues)
	}
}
