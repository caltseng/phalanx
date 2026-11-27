package application

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestFlagDerivationsRespectPerFamilyBoundWithoutLifecycleEffects(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	now := time.Now().UTC()
	app.Clock = func() time.Time { return now }
	stale := timestamp(now.Add(-16 * time.Minute))
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for index := range 210 {
		id := fmt.Sprintf("stale-%03d", index)
		if _, err := tx.Exec(`INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at)
			VALUES (?,?,'synthetic-charter','active',?,?)`, id, id, stale, stale); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	before, err := app.Inspect(context.Background(), "up")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := app.Flags(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Flags []FlagView `json:"flags"`
	}
	if err := json.Unmarshal(responseJSON(t, raw), &result); err != nil {
		t.Fatal(err)
	}
	staleCount := 0
	for _, flag := range result.Flags {
		if flag.Kind == "captain_stale" {
			staleCount++
		}
	}
	if staleCount != 200 {
		t.Fatal("bounded stale-Captain projection changed", staleCount)
	}
	after, err := app.Inspect(context.Background(), "up")
	if err != nil || string(responseJSON(t, after)) != string(responseJSON(t, before)) {
		t.Fatal("deriving flags changed work state/history/capacity", err)
	}
}

func TestScopedWriterFlagsDoNotLeakUnrelatedStoredFlags(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-flags-0001", TokenDigest: contract.Digest("flag-token")})
	if err != nil {
		t.Fatal(err)
	}
	execution, writerID, attempt := decodeBinding(t, registered)
	for _, request := range []contract.Flag{
		{FlagKey: contract.FlagKey{Kind: "scoped", ItemID: "up", ExecutionID: "execution"}, Reason: "Owned", Recovery: "Owned action"},
		{FlagKey: contract.FlagKey{Kind: "unrelated", ItemID: "down", ExecutionID: "another"}, Reason: "Other", Recovery: "Other action"},
		{FlagKey: contract.FlagKey{Kind: "global"}, Reason: "Global", Recovery: "Global action"},
	} {
		if _, err := app.RaiseFlag(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := app.CurrentWriter(ctx, writerID)
	if err != nil {
		t.Fatal(err)
	}
	var response struct {
		Flags []FlagView `json:"flags"`
	}
	if err := json.Unmarshal(responseJSON(t, raw), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.Flags) != 1 || response.Flags[0].Kind != "scoped" {
		t.Fatal("current writer received flags from another execution or global scope", string(responseJSON(t, raw)))
	}
	var version int64
	var ended *string
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT ended_at FROM attempts WHERE id=?", *attempt).Scan(&ended); err != nil {
		t.Fatal(err)
	}
	if version != execution.Version || ended != nil {
		t.Fatal("flag writes/reads changed lifecycle or active ownership", version, ended)
	}
}

func TestPickupKeepsEmptyPersistedDependencyWaitAsBlocker(t *testing.T) {
	state, err := contract.Canonical(Start(MergedTarget, ReadyMode))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	ready, empty := now.UnixMilli()-1, "[]"
	row := sqlite.QueueProjection{ID: "execution", ItemID: "item", RepositoryID: "fixture", Stage: "development",
		StateJSON: state, ReadySince: &ready, DependencyWait: &empty}
	current := pickupContext{admissionContext: admissionContext{Capacity: sqlite.DispatchSettings{MaxWriters: 2, MaxBuildingWriters: 1}},
		Captains:  []pickupCaptain{{ID: "captain", Repositories: map[string]*string{"fixture": nil}}},
		Admission: map[Stage]*string{Development: nil}, Holds: map[string]string{}}
	decision, err := pickupDecision(row, now, current)
	if err != nil || decision.Eligible || decision.Code != "dependency" ||
		decision.Reason == nil || *decision.Reason != "Persisted dependency wait requires reconciliation" {
		t.Fatal("empty non-null dependency marker became permission to dispatch", decision, err)
	}
	row.DependencyWait = nil
	decision, err = pickupDecision(row, now, current)
	if err != nil || !decision.Eligible || len(decision.CaptainIDs) != 1 || decision.CaptainIDs[0] != "captain" {
		t.Fatal("cleared dependency marker did not restore scoped pickup", decision, err)
	}
}
