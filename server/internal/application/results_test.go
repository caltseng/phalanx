package application

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

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
		t.Fatal("development did not continue the same writer into self-review", string(review))
	}
	replay, err := app.WriterResult(ctx, "up", "execution", id, dev, &key)
	if err != nil || string(replay) != string(review) {
		t.Fatal("stage receipt replay re-claimed or rewrote the original binding", string(replay), err)
	}
	published, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "4", AttemptID: reviewAttempt, Stage: "self_review", Outcome: "succeeded",
		ReviewedCandidate: &head}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, publicationAttempt := decodeBinding(t, published)
	if sameID != id || row.Version != 6 || publicationAttempt == nil {
		t.Fatal("review did not continue the same writer into publication", string(published))
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
		t.Fatal("publication did not retain an attempt-free delivery binding", string(delivering))
	}
	question := &contract.Question{Prompt: "Owner direction", Options: []string{"Retry", "Stop"}, Handoff: "Retain this PR"}
	waiting, err := app.WriterResult(ctx, "up", "execution", id, contract.WriterResult{
		ExpectedVersion: "7", Stage: "delivery", Outcome: "needs_input", Question: question}, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, _ = decodeBinding(t, waiting)
	if sameID != id || row.Version != 8 {
		t.Fatal("attempt-free question changed writer identity", string(waiting))
	}
	reply, err := app.Reply(ctx, "up", "execution", contract.Reply{ExpectedVersion: "8", Answer: "Retry", AnsweredBy: "owner"}, "owner-reply")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(reply, &row); err != nil || row.Version != 9 {
		t.Fatal("owner reply did not queue the current stage", string(reply), err)
	}
	resumed, err := app.WriterNext(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	row, sameID, deliveryAttempt = decodeBinding(t, resumed)
	if sameID != id || row.Version != 10 || deliveryAttempt == nil {
		t.Fatal("owner reply did not resume the retained writer", string(resumed))
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
