package application

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func TestObservationStoragePreservesCanonicalPayload(t *testing.T) {
	stamp := contract.AwareTime{Time: time.Date(2026, 1, 2, 3, 4, 5, 123456000, time.UTC)}
	huge := json.Number("90071992547409931234567890")
	request := contract.Observation{ObservedAt: stamp, TargetBase: pointer("stale-base"),
		Checks: pointer("satisfied"), CheckReasons: []string{"snow \u96ea"}, Feedback: []string{},
		PR: &contract.ObservedPull{Number: "1", State: "open", HeadRef: "branch", HeadSHA: "head",
			BaseRef: "main", BaseSHA: "base", Additions: &huge}}
	payload := observationStorage(request, "https://github.com/example/repo/pull/1", pointer("main"))
	raw, err := contract.Canonical(payload)
	if err != nil {
		t.Fatal(err)
	}
	expected := `{"check_reasons":["snow \u96ea"],"checks":"satisfied","deployment":null,"duplicate_count":null,"feedback":[],"merged_branch":false,"pr":{"additions":90071992547409931234567890,"base_ref":"main","base_sha":"base","deletions":null,"draft":false,"head_ref":"branch","head_sha":"head","merge_commit":null,"merge_method":null,"mergeable":null,"merged":false,"merged_at":null,"merged_head_ancestor":null,"number":1,"state":"open","url":"https://github.com/example/repo/pull/1"},"target_base":"main"}`
	if raw != expected || contract.Digest(raw) != contract.Digest(expected) {
		t.Fatalf("stored payload/digest changed:\n%s", raw)
	}
	var restored observationPayload
	if err := json.Unmarshal([]byte(raw), &restored); err != nil {
		t.Fatal(err)
	}
	if restored.PR == nil || restored.PR.ObservedPull == nil || *restored.PR.Additions != huge ||
		*request.TargetBase != "stale-base" || strings.Contains(raw, "observed_at") {
		t.Fatal("typed storage lost exact fields or mutated the request", restored, request)
	}
	empty, err := contract.Canonical(observationStorage(contract.Observation{
		CheckReasons: []string{}, Feedback: []string{}}, "", nil))
	if err != nil {
		t.Fatal(err)
	}
	if empty != `{"check_reasons":[],"checks":null,"deployment":null,"duplicate_count":null,"feedback":[],"merged_branch":false,"pr":null,"target_base":null}` {
		t.Fatal(empty)
	}
}
