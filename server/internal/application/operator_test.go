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
