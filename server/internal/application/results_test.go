package application

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestObservationAndDeliveryRollbackTogetherThenReplay(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-observe-0001", TokenDigest: contract.Digest("observe-token")})
	if err != nil {
		t.Fatal(err)
	}
	row, writerID, attempt := decodeBinding(t, registered)
	head, base, url := "a0123456789012345678901234567890123456789", "b0123456789012345678901234567890123456789", "https://github.com/example/fixture/pull/1"
	state := Start(ReadyTarget, ReadyMode)
	state.Stage, state.Status, state.Candidate, state.PrimaryPR = Delivery, WaitingForExternal, &head, &url
	state.Wait = &Wait{"Await current PR", "observation"}
	raw, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=? WHERE id='execution'; UPDATE writers SET building=0", raw); err != nil {
		t.Fatal(err)
	}
	request := contract.Observation{ObservedAt: contract.AwareTime{Time: time.Now().UTC()},
		PR: &contract.ObservedPull{Number: "1", State: "open", HeadRef: itemBranch("up"), HeadSHA: head,
			BaseRef: "main", BaseSHA: base}, Checks: pointer("satisfied"), TargetBase: pointer("main"),
		CheckReasons: []string{}, Feedback: []string{}}
	if _, err := db.Exec(`CREATE TRIGGER abort_observation_history BEFORE INSERT ON transition_history
		WHEN NEW.event='pr_observed' BEGIN SELECT RAISE(ABORT,'injected observation-history failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := app.RecordObservation(ctx, "up", "execution", request); err == nil {
		t.Fatal("history failure was hidden")
	}
	var observations, history int
	var version int64
	var ended *string
	if err := db.QueryRow("SELECT count(*) FROM github_observations").Scan(&observations); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT ended_at FROM writers WHERE id=?", writerID).Scan(&ended); err != nil {
		t.Fatal(err)
	}
	if observations != 0 || version != row.Version || ended != nil {
		t.Fatal("observation/apply failure leaked durable delivery effects", observations, version, ended)
	}
	if _, err := db.Exec("DROP TRIGGER abort_observation_history"); err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	failures := make([]error, 2)
	responses := make([]any, 2)
	for index := range 2 {
		group.Add(1)
		go func(index int) {
			defer group.Done()
			responses[index], failures[index] = app.RecordObservation(ctx, "up", "execution", request)
		}(index)
	}
	group.Wait()
	recorded := 0
	for index, err := range failures {
		if err != nil {
			t.Fatal(err)
		}
		var response struct{ Recorded bool }
		if err := json.Unmarshal(responseJSON(t, responses[index]), &response); err != nil {
			t.Fatal(err)
		}
		if response.Recorded {
			recorded++
		}
	}
	if err := db.QueryRow("SELECT count(*) FROM github_observations").Scan(&observations); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM transition_history WHERE event='pr_observed'").Scan(&history); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT ended_at FROM attempts WHERE id=?", *attempt).Scan(&ended); err != nil {
		t.Fatal(err)
	}
	if recorded != 1 || observations != 1 || history != 1 || version != row.Version+1 || ended == nil {
		t.Fatal("racing replay duplicated delivery history or retained the current attempt", recorded, observations, history, version, ended)
	}
	var building bool
	if err := db.QueryRow("SELECT ended_at,building FROM writers WHERE id=?", writerID).Scan(&ended, &building); err != nil {
		t.Fatal(err)
	}
	if ended == nil || building {
		t.Fatal("verified delivery retained writer ownership or Building", ended, building)
	}
}

func TestWriterResultsContinueSameWriterThroughPublicationAndReply(t *testing.T) {
	app, _ := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-results-0001", TokenDigest: contract.Digest("result-token")})
	if err != nil {
		t.Fatal(err)
	}
	_, id, firstAttempt := decodeBinding(t, registered)
	head := "a0123456789012345678901234567890123456789"
	dev := contract.WriterResult{ExpectedVersion: "2", AttemptID: firstAttempt, Stage: "development",
		Outcome: "succeeded", Candidate: &head}
	key := "development-result"
	review, err := app.WriterResult(ctx, "up", "execution", id, dev, &key)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, reviewAttempt := decodeBinding(t, review)
	if sameID != id || row.Version != 4 || reviewAttempt == nil || *reviewAttempt == *firstAttempt {
		t.Fatal("development did not continue the same writer into self-review", string(responseJSON(t, review)))
	}
	replay, err := app.WriterResult(ctx, "up", "execution", id, dev, &key)
	if err != nil || string(responseJSON(t, replay)) != string(responseJSON(t, review)) {
		t.Fatal("stage receipt replay re-claimed or rewrote the original binding", string(responseJSON(t, replay)), err)
	}
	published, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "4", AttemptID: reviewAttempt, Stage: "self_review", Outcome: "succeeded",
		ReviewedCandidate: &head}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, publicationAttempt := decodeBinding(t, published)
	if sameID != id || row.Version != 6 || publicationAttempt == nil {
		t.Fatal("review did not continue the same writer into publication", string(responseJSON(t, published)))
	}
	if err := app.Store.Read(ctx, func(tx *sqlite.Tx) error {
		writer, err := tx.WriterRecord(id)
		if err == nil && (writer.Building || writer.EndedAt != nil) {
			t.Fatal("successful review did not return Building while retaining Total", writer.Building, writer.EndedAt)
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
	url := "https://github.com/example/fixture/pull/1"
	delivering, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "6", AttemptID: publicationAttempt, Stage: "publication", Outcome: "succeeded", PRURL: &url}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, deliveryAttempt := decodeBinding(t, delivering)
	if sameID != id || row.Version != 7 || deliveryAttempt != nil {
		t.Fatal("publication did not retain an attempt-free delivery binding", string(responseJSON(t, delivering)))
	}
	question := &contract.Question{Prompt: "Owner direction", Options: []string{"Retry", "Stop"}, Handoff: "Retain this PR"}
	waiting, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "7", Stage: "delivery", Outcome: "needs_input", Question: question}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, _ = decodeBinding(t, waiting)
	if sameID != id || row.Version != 8 {
		t.Fatal("attempt-free question changed writer identity", string(responseJSON(t, waiting)))
	}
	reply, err := app.Reply(ctx, "up", "execution", contract.Reply{ExpectedVersion: "8", Answer: "Retry", AnsweredBy: "owner"}, "owner-reply")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(responseJSON(t, reply), &row); err != nil || row.Version != 9 {
		t.Fatal("owner reply did not queue the current stage", string(responseJSON(t, reply)), err)
	}
	resumed, err := app.WriterNext(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, deliveryAttempt = decodeBinding(t, resumed)
	if sameID != id || row.Version != 10 || deliveryAttempt == nil {
		t.Fatal("owner reply did not resume the retained writer", string(responseJSON(t, resumed)))
	}
}

func TestWriterResultRequiresStructuredQuestionAndKeepsFailureAtomic(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-input-0001", TokenDigest: contract.Digest("input-token")})
	if err != nil {
		t.Fatal(err)
	}
	_, id, attempt := decodeBinding(t, registered)
	if _, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "2", AttemptID: attempt, Stage: "development", Outcome: "needs_input",
		Wait: &contract.WaitInput{Reason: "Missing authority", Trigger: "owner reply"}}, nil); err == nil {
		t.Fatal("unstructured input wait was accepted")
	}
	var version int64
	var ended *string
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT ended_at FROM attempts WHERE id=?", *attempt).Scan(&ended); err != nil {
		t.Fatal(err)
	}
	if version != 2 || ended != nil {
		t.Fatal("refused input result changed history or ended the active attempt", version, ended)
	}
}

func TestResultReceiptFailureRollsBackStageClaimAndBuildingRelease(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-receipt-0001", TokenDigest: contract.Digest("receipt-token")})
	if err != nil {
		t.Fatal(err)
	}
	_, writerID, development := decodeBinding(t, registered)
	head := "a0123456789012345678901234567890123456789"
	reviewed, err := app.WriterResult(ctx, "up", "execution", writerID, contract.WriterResult{
		ExpectedVersion: "2", AttemptID: development, Stage: "development", Outcome: "succeeded", Candidate: &head}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, _, attempt := decodeBinding(t, reviewed)
	key := "self-review-receipt"
	request := contract.WriterResult{ExpectedVersion: "4", AttemptID: attempt, Stage: "self_review",
		Outcome: "succeeded", ReviewedCandidate: &head}
	if _, err := db.Exec(`CREATE TRIGGER abort_stage_receipt BEFORE INSERT ON request_receipts
			WHEN NEW.scope LIKE 'result:%' BEGIN SELECT RAISE(ABORT,'injected receipt insert failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := app.WriterResult(ctx, "up", "execution", writerID, request, &key); err == nil {
		t.Fatal("receipt failure was hidden")
	}
	var version int64
	var attempts, history, receipts int
	var building bool
	var current *string
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM attempts").Scan(&attempts); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM transition_history").Scan(&history); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM request_receipts").Scan(&receipts); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT building,attempt_id FROM writers WHERE id=?", writerID).Scan(&building, &current); err != nil {
		t.Fatal(err)
	}
	if version != row.Version || attempts != 2 || history != 3 || receipts != 0 || !building || !sameText(current, attempt) {
		t.Fatal("receipt fault retained transition/claim/history/capacity effects", version, attempts, history, receipts, building, current)
	}
	if _, err := db.Exec("DROP TRIGGER abort_stage_receipt"); err != nil {
		t.Fatal(err)
	}
	published, err := app.WriterResult(ctx, "up", "execution", writerID, request, &key)
	if err != nil {
		t.Fatal(err)
	}
	row, _, _ = decodeBinding(t, published)
	if row.Version != 6 {
		t.Fatal("retry did not advance exactly once into publication", string(responseJSON(t, published)))
	}
}
