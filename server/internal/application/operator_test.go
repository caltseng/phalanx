package application

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestReasonTextRedactsBeforeTruncating(t *testing.T) {
	for _, test := range []struct {
		name, input, want string
		limit             int
	}{
		{"token", "push ghp_private github_pat_private failed", "push [redacted] [redacted] failed", 200},
		{"bearer", "Authorization: BeArEr private", "Authorization: [redacted]", 200},
		{"basic", "Basic private", "[redacted]", 200},
		{"assignments", `runner_token="two words" PASSWORD='private' api-key=private lease: private`, "[redacted] [redacted] [redacted] [redacted]", 200},
		{"quoted key", `{"secret_key": "private"}`, `{"[redacted]}`, 200},
		{"overlap", `token="ghp_private other"`, "[redacted]", 200},
		{"word boundaries", "xghp_private αBearer private _token=private", "xghp_private αBearer private _token=private", 200},
		{"unicode prefix", "雪: Bearer private", "雪: [redacted]", 200},
		{"normalization", " \tBearer\u00a0private\nfailed ", "[redacted] failed", 200},
		{"truncate", "雪 ghp_private failed", "雪 [redact…", 10},
		{"ordinary", "Push rejected: permission denied", "Push rejected: permission denied", 200},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := reasonText(test.input, test.limit); got != test.want {
				t.Fatalf("reasonText = %q, want %q", got, test.want)
			}
		})
	}
}

func TestStoredStateDefaultsAndReadOnlyNullMode(t *testing.T) {
	for _, test := range []struct {
		name, fields string
		status       Status
		mode         PublicationMode
		nullMode     bool
		invalid      bool
	}{
		{name: "omitted", status: Queued, mode: ReadyMode},
		{name: "explicit", fields: `,"status":"running","publication_mode":"draft"`, status: Running, mode: DraftMode},
		{name: "null mode", fields: `,"publication_mode":null`, status: Queued, mode: ReadyMode, nullMode: true},
		{name: "last null mode", fields: `,"publication_mode":"draft","publication_mode":null`, status: Queued, mode: ReadyMode, nullMode: true},
		{name: "null then explicit", fields: `,"status":null,"status":"running","publication_mode":null,"publication_mode":"draft"`, status: Running, mode: DraftMode},
		{name: "null status", fields: `,"status":null`, invalid: true},
		{name: "invalid mode", fields: `,"publication_mode":"invalid"`, invalid: true},
		{name: "invalid wait", fields: `,"wait":{"reason":"","trigger":"resume"}`, invalid: true},
		{name: "invalid intent", fields: `,"stopping":"invalid"`, invalid: true},
		{name: "terminal without output", fields: `,"status":"completed"`, invalid: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			data := json.RawMessage(`{"target":"checked_in","stage":"development"` + test.fields + `}`)
			state, mode, err := decodeStoredState(data, true)
			if (err != nil) != test.invalid {
				t.Fatalf("read-only state error = %v", err)
			}
			if !test.invalid && (state.Status != test.status || state.PublicationMode != test.mode ||
				(mode == nil) != test.nullMode || mode != nil && *mode != test.mode) {
				t.Fatalf("state=%+v mode=%v", state, mode)
			}
			_, err = readState(data)
			if (err != nil) != (test.invalid || test.nullMode) {
				t.Fatalf("authoritative state error = %v", err)
			}
		})
	}
}

func TestOperatorWaitReusesDecodedState(t *testing.T) {
	state := Start(MergedTarget, ReadyMode)
	stopping := Cancelled
	state.Stopping = &stopping
	row := sqlite.OperatorRow{Status: "queued", StateJSON: "must not decode this again"}
	wait, _, decision, err := operatorWait(row, state, time.Now(), operatorContext{})
	if err != nil || wait == nil || decision == nil || decision.Code != "closing" {
		t.Fatalf("wait=%+v decision=%+v err=%v", wait, decision, err)
	}
}

func TestOperatorSummaryDefersDeploymentDecodeToResponseProjection(t *testing.T) {
	goal, deployment := "Stored deployment", `{"unknown":999999999999999999999999999999,"ratio":1e0}`
	row := sqlite.OperatorRow{
		StateJSON: `{"target":"checked_in","stage":"development","publication_mode":null}`,
		Stage:     "development", Status: "queued", Goal: &goal, DeploymentJSON: &deployment,
	}
	item, err := operatorSummary(row, time.Now(), operatorContext{})
	if err != nil {
		t.Fatal(err)
	}
	if item.PublicationMode != nil || string(item.Deployment) != deployment || item.Pickup == nil {
		t.Fatalf("null mode, raw deployment or pickup changed: %+v", item)
	}
	body, err := responseValue(item)
	if err != nil {
		t.Fatal(err)
	}
	text := string(responseJSON(t, body))
	if !strings.Contains(text, `"unknown":999999999999999999999999999999`) ||
		!strings.Contains(text, `"publication_mode":null`) ||
		strings.Contains(text, `"acceptance_criteria"`) {
		t.Fatal("summary projection changed numeric/null/optional fields", text)
	}
	row.DeploymentJSON = &goal
	item, err = operatorSummary(row, time.Now(), operatorContext{})
	if err != nil {
		t.Fatal("summary parsed deployment before the response boundary", err)
	}
	if _, err := responseValue(item); err == nil {
		t.Fatal("invalid stored deployment did not fail response projection")
	}
}

func TestOperatorMetricsRetainSQLiteKindsAndExactSums(t *testing.T) {
	_, db := writerStore(t, 1, 1)
	var integer, real, absent *sqlite.Number
	if err := db.QueryRow("SELECT 9223372036854775807,1.0,NULL").Scan(&integer, &real, &absent); err != nil {
		t.Fatal(err)
	}
	sum, err := contract.SumNumbers([]json.Number{json.Number(*integer), json.Number(*integer)})
	if err != nil || sum != json.Number("18446744073709551614") || absent != nil {
		t.Fatal("integer overflow or unknown metric changed", sum, absent, err)
	}
	sum, err = contract.SumNumbers([]json.Number{"9007199254740992", json.Number(*real), "1"})
	if err != nil || sum != float64(9007199254740992) {
		t.Fatal("REAL promotion stopped rounding in row order", sum, err)
	}
}

func TestFilteredOperatorPageContinuesAfterExactCandidateWindow(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	now := time.Now().UTC()
	app.Clock = func() time.Time { return now }
	if _, err := db.Exec(`INSERT INTO operator_change VALUES ('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
		INSERT INTO issue_scout_settings(id) VALUES (1)`); err != nil {
		t.Fatal(err)
	}
	state, err := contract.Canonical(Start(MergedTarget, ReadyMode))
	if err != nil {
		t.Fatal(err)
	}
	stamp := timestamp(now)
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for index := range 2100 {
		item, execution := fmt.Sprintf("%032x", index+0x10000), fmt.Sprintf("%032x", index+0x20000)
		goal := "Other work"
		if index == 2005 {
			goal = "Find this needle"
		}
		request, err := contract.Canonical(map[string]any{"goal": goal, "target": "checked_in"})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec("INSERT INTO work_items(id,repository_id,request_json,created_at) VALUES (?,'fixture',?,?)",
			item, request, stamp); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(`INSERT INTO executions
			(id,item_id,version,state_json,created_at,updated_at,queue_repository,ready_since)
			VALUES (?,?,1,?,?,?,'fixture',?)`, execution, item, state, stamp, stamp, now.UnixMilli()); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec("INSERT INTO transition_history VALUES (?,1,NULL,'intake','{}',?,?)", execution, state, stamp); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	query := "needle"
	request := contract.OperatorQuery{Kind: "open", Limit: 1, Query: &query}
	response, err := app.OperatorList(context.Background(), request, ConditionalRead{Path: "/v1/operator/executions", Query: "q=needle&limit=1"})
	if err != nil {
		t.Fatal(err)
	}
	var first struct {
		Items []map[string]any `json:"items"`
		Next  *string          `json:"next_cursor"`
	}
	if err := json.Unmarshal(responseJSON(t, response.Body), &first); err != nil {
		t.Fatal(err)
	}
	if len(first.Items) != 0 || first.Next == nil {
		t.Fatal("empty filtered window dropped the bounded continuation", string(responseJSON(t, response.Body)))
	}
	request.Cursor = first.Next
	response, err = app.OperatorList(context.Background(), request, ConditionalRead{Path: "/v1/operator/executions", Query: "q=needle&limit=1&cursor=" + *first.Next})
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(responseJSON(t, response.Body), &first); err != nil {
		t.Fatal(err)
	}
	if len(first.Items) != 1 || first.Items[0]["goal"] != "Find this needle" || first.Next != nil {
		t.Fatal("continuation skipped a match beyond the 2000-candidate window", string(responseJSON(t, response.Body)))
	}
}

func TestOperatorValidatorHashesEveryInputDeterministically(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	now := time.Now().UTC()
	if _, err := db.Exec(`INSERT INTO operator_change VALUES ('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')`); err != nil {
		t.Fatal(err)
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		baseline := ""
		for round := range 20 {
			current := operatorContext{
				Pickup: pickupContext{
					Holds: map[string]string{"second": "held", "first": "held"},
					Captains: []pickupCaptain{{ID: "captain", Repositories: map[string]*string{"fixture": nil},
						RawPickup: json.RawMessage(`{"repositories":{"fixture":null},"opaque":{"number":999999999999999999999}}`)}},
				},
				Sessions: map[sessionKey]string{
					{"runtime", "second"}: "available", {"", "first"}: "missing",
				},
			}
			condition := ConditionalRead{Path: "/v1/operator/executions", Query: "limit=1"}
			scouting := map[string]any{"state": "due", "counts": map[string]int64{"needs_owner": 1}}
			tag, matched, err := operatorValidator(tx, now, current, condition, scouting)
			if err != nil {
				return err
			}
			if matched || round > 0 && tag != baseline {
				t.Fatal("unchanged maps produced a different validator", tag, baseline)
			}
			baseline = tag
			if round > 0 {
				continue
			}
			for _, test := range []struct {
				name   string
				change func()
				undo   func()
			}{
				{"path", func() { condition.Path += "/other" }, func() { condition.Path = "/v1/operator/executions" }},
				{"query", func() { condition.Query = "limit=2" }, func() { condition.Query = "limit=1" }},
				{"hold", func() { current.Pickup.Holds["third"] = "held" }, func() { delete(current.Pickup.Holds, "third") }},
				{"session availability", func() { current.Sessions[sessionKey{"", "first"}] = "available" },
					func() { current.Sessions[sessionKey{"", "first"}] = "missing" }},
				{"session runtime", func() { current.Sessions[sessionKey{"other", "first"}] = "missing" },
					func() { delete(current.Sessions, sessionKey{"other", "first"}) }},
				{"captain identity", func() { current.Pickup.Captains[0].ID = "other" },
					func() { current.Pickup.Captains[0].ID = "captain" }},
				{"opaque pickup", func() {
					current.Pickup.Captains[0].RawPickup = json.RawMessage(`{"repositories":{"fixture":null},"opaque":{"number":999999999999999999998}}`)
				},
					func() {
						current.Pickup.Captains[0].RawPickup = json.RawMessage(`{"repositories":{"fixture":null},"opaque":{"number":999999999999999999999}}`)
					}},
				{"scouting", func() { scouting["state"] = "running" }, func() { scouting["state"] = "due" }},
			} {
				t.Run(test.name, func(t *testing.T) {
					test.change()
					changed, _, err := operatorValidator(tx, now, current, condition, scouting)
					if err != nil || changed == baseline {
						t.Fatal("changed input did not invalidate", changed, err)
					}
					test.undo()
					restored, _, err := operatorValidator(tx, now, current, condition, scouting)
					if err != nil || restored != baseline {
						t.Fatal("restored input did not restore validator", restored, err)
					}
				})
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}
func TestOperatorValidatorIgnoresTickingAgesButKeepsDeadlinesAndOpaquePickup(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	now := time.Now().UTC().Truncate(time.Millisecond)
	if _, err := db.Exec(`INSERT INTO operator_change VALUES ('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
		UPDATE executions SET ready_since=? WHERE id='execution'`, now.Add(time.Second).UnixMilli()); err != nil {
		t.Fatal(err)
	}
	condition := ConditionalRead{Path: "/v1/operator/executions"}
	tags := []string{}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		for _, at := range []time.Time{now, now.Add(500 * time.Millisecond), now.Add(time.Second)} {
			current, err := operatorSnapshot(tx, at)
			if err != nil {
				return err
			}
			tag, _, err := operatorValidator(tx, at, current, condition, nil)
			if err != nil {
				return err
			}
			tags = append(tags, tag)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if tags[0] != tags[1] || tags[0] == tags[2] {
		t.Fatal("weak validator changed on age tick or ignored a newly due scheduling deadline", tags)
	}
	if _, err := db.Exec(`UPDATE captains SET pickup_json='{"repositories":{},"runner_id":null}' WHERE id='captain';
		UPDATE operator_change SET token='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'`); err != nil {
		t.Fatal(err)
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		current, err := operatorSnapshot(tx, now.Add(time.Second))
		if err != nil {
			return err
		}
		tag, _, err := operatorValidator(tx, now.Add(time.Second), current, condition, nil)
		if err == nil && tag == tags[2] {
			t.Fatal("opaque retained pickup value disappeared from validator material")
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}
