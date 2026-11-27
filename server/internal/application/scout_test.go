package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func scoutStore(t *testing.T) (*App, *sql.DB) {
	t.Helper()
	app, db := writerStore(t, 2, 1)
	if _, err := db.Exec(`INSERT INTO issue_scout_settings(id) VALUES (1);
		UPDATE captains SET pickup_json='{"repositories":{"fixture":null}}' WHERE id='captain'`); err != nil {
		t.Fatal(err)
	}
	if _, err := app.ConfigureScout(context.Background(), contract.ScoutSettings{ExpectedRevision: "0",
		Enabled: true, CadenceMinutes: 60, AuthorLogin: "synthetic-author", AuthorID: "9001",
		Repositories: []string{"fixture"}}); err != nil {
		t.Fatal(err)
	}
	return app, db
}

func decodeScoutClaim(t *testing.T, raw json.RawMessage) *sqlite.ScoutPass {
	t.Helper()
	var response struct {
		Claim *sqlite.ScoutPass `json:"claim"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		t.Fatal(err)
	}
	return response.Claim
}

func nativeScoutPage(row *sqlite.ScoutPass) contract.ScoutPage {
	author, authorID := "synthetic-author", json.Number("9001")
	return contract.ScoutPage{ClaimID: *row.ClaimID, RepositoryID: "fixture", Page: json.Number("1"),
		VerifiedLogin: author, VerifiedID: authorID, Issues: []contract.ScoutIssue{
			{Number: "1", AuthorLogin: &author, AuthorID: &authorID, State: "open",
				Title: "Synthetic scout work", Body: "Synthetic source snow \u96ea",
				UpdatedAt: contract.DateTime{Time: time.Now().UTC(), Aware: true}},
		}}
}

func TestScoutIntakeSourceCursorAndReceiptsRollbackThenRaceTogether(t *testing.T) {
	app, db := scoutStore(t)
	ctx := context.Background()
	raw, err := app.ClaimScout(ctx, contract.ScoutClaim{CaptainID: "captain"})
	if err != nil {
		t.Fatal(err)
	}
	claim := decodeScoutClaim(t, raw)
	if claim == nil {
		t.Fatal("no actual scout allocation", string(raw))
	}
	request := nativeScoutPage(claim)
	var baselineItems int
	if err := db.QueryRow("SELECT count(*) FROM work_items").Scan(&baselineItems); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TRIGGER abort_scout_cursor BEFORE UPDATE OF repository_index ON issue_scout_passes
		BEGIN SELECT RAISE(ABORT,'injected cursor failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := app.ScoutPage(ctx, claim.ID, request); err == nil {
		t.Fatal("cursor failure was hidden")
	}
	var items, sources, receipts int
	var currentClaim *string
	if err := db.QueryRow("SELECT count(*) FROM work_items").Scan(&items); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM issue_sources").Scan(&sources); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM request_receipts").Scan(&receipts); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT claim_id FROM issue_scout_passes WHERE id=?", claim.ID).Scan(&currentClaim); err != nil {
		t.Fatal(err)
	}
	if items != baselineItems || sources != 0 || receipts != 0 || !sameText(currentClaim, claim.ClaimID) {
		t.Fatal("scout page fault retained partial intake/source/receipt/cursor effects", items, sources, receipts, currentClaim)
	}
	if _, err := db.Exec("DROP TRIGGER abort_scout_cursor"); err != nil {
		t.Fatal(err)
	}
	results := make([]json.RawMessage, 2)
	failures := make([]error, 2)
	var writers sync.WaitGroup
	for index := range 2 {
		writers.Add(1)
		go func(index int) {
			defer writers.Done()
			results[index], failures[index] = app.ScoutPage(ctx, claim.ID, request)
		}(index)
	}
	writers.Wait()
	for _, err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	if string(results[0]) != string(results[1]) {
		t.Fatal("page receipt replay replaced its captured response", string(results[0]), string(results[1]))
	}
	if err := db.QueryRow("SELECT count(*) FROM work_items").Scan(&items); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM issue_sources").Scan(&sources); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM request_receipts").Scan(&receipts); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT claim_id FROM issue_scout_passes WHERE id=?", claim.ID).Scan(&currentClaim); err != nil {
		t.Fatal(err)
	}
	if items != baselineItems+1 || sources != 1 || receipts != 2 || currentClaim != nil {
		t.Fatal("racing replay duplicated intake or failed to release the page claim", items, sources, receipts, currentClaim)
	}
}

func TestScoutSingleClaimHandoffAndSettingsSupersedeFenceOldPages(t *testing.T) {
	app, db := scoutStore(t)
	ctx := context.Background()
	now := timestamp(time.Now().UTC())
	if _, err := db.Exec(`INSERT INTO captains(id,session_ref,charter_revision,pickup_json,state,created_at,heartbeat_at)
		VALUES ('successor','synthetic-successor','charter','{"repositories":{"fixture":null}}','active',?,?)`, now, now); err != nil {
		t.Fatal(err)
	}
	captains := []string{"captain", "successor"}
	responses := make([]json.RawMessage, 2)
	failures := make([]error, 2)
	var workers sync.WaitGroup
	for index, captain := range captains {
		workers.Add(1)
		go func(index int, captain string) {
			defer workers.Done()
			responses[index], failures[index] = app.ClaimScout(ctx, contract.ScoutClaim{CaptainID: captain})
		}(index, captain)
	}
	workers.Wait()
	var claim *sqlite.ScoutPass
	count := 0
	for index, err := range failures {
		if err != nil {
			t.Fatal(err)
		}
		if current := decodeScoutClaim(t, responses[index]); current != nil {
			claim, count = current, count+1
		}
	}
	if count != 1 {
		t.Fatal("racing Captains created competing page claims", responses)
	}
	oldPage := nativeScoutPage(claim)
	owner, successor := *claim.CaptainID, "captain"
	if owner == successor {
		successor = "successor"
	}
	if _, err := db.Exec("UPDATE captains SET state='ending' WHERE id=?", owner); err != nil {
		t.Fatal(err)
	}
	if _, err := app.ScoutPage(ctx, claim.ID, oldPage); err == nil {
		t.Fatal("ending Captain completed its old page")
	}
	newRaw, err := app.ClaimScout(ctx, contract.ScoutClaim{CaptainID: successor})
	if err != nil {
		t.Fatal(err)
	}
	current := decodeScoutClaim(t, newRaw)
	if current == nil || current.ID != claim.ID || sameText(current.ClaimID, claim.ClaimID) || *current.CaptainID != successor {
		t.Fatal("successor did not reclaim the same durable cursor", string(newRaw))
	}
	var conflict *Conflict
	if _, err := app.ScoutPage(ctx, claim.ID, oldPage); !errors.As(err, &conflict) {
		t.Fatal("superseded claim was not fenced", err)
	}
	if _, err := app.ConfigureScout(ctx, contract.ScoutSettings{ExpectedRevision: "1", Enabled: true,
		CadenceMinutes: 60, AuthorLogin: "synthetic-author", AuthorID: "9001", Repositories: []string{"fixture"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := app.ScoutPage(ctx, current.ID, nativeScoutPage(current)); !errors.As(err, &conflict) {
		t.Fatal("settings supersession did not fence the prior pass", err)
	}
	var sourceCount int
	if err := db.QueryRow("SELECT count(*) FROM issue_sources").Scan(&sourceCount); err != nil || sourceCount != 0 {
		t.Fatal("refused old pages changed source or intake state", sourceCount, err)
	}
}
