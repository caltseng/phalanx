package application

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func responseJSON(t *testing.T, value any) []byte {
	t.Helper()
	switch value := value.(type) {
	case []byte:
		return value
	case json.RawMessage:
		return value
	}
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestLegacyReceiptDigestsAndSnapshotsReplayUnchanged(t *testing.T) {
	store, _ := dependencyStore(t)
	app := New(store)
	request, issues := contract.ValidateIntake(map[string]any{"repository_id": "fixture", "goal": "Legacy"},
		[]string{"repository_id", "goal"})
	if len(issues) != 0 {
		t.Fatal(issues)
	}
	const snapshot = `{"id":"legacy","note":"\u00e9 <>&","number":1.0}`
	for _, digest := range []string{
		"791935e974e970ab8cab68ccceda94588076f93da0e0aa34391e76c9f45e9ac8",
		"43f3cd4fa1d344517d9e0ebb53723e08f6fb4dea1a03fd5fb5459d92a5f65b8e",
	} {
		key := digest
		if err := store.Write(context.Background(), func(tx *sqlite.Tx) error {
			return tx.SaveReceipt("intake", key, digest, snapshot)
		}); err != nil {
			t.Fatal(err)
		}
		result, err := app.Intake(context.Background(), request, &key)
		if err != nil {
			t.Fatal("legacy intake key stopped replaying", err)
		}
		text, err := contract.Canonical(result)
		if err != nil || text != snapshot {
			t.Fatal("captured receipt response changed", text, err)
		}
		if err := store.Read(context.Background(), func(tx *sqlite.Tx) error {
			receipt, err := tx.Receipt("intake", key)
			if err == nil && (receipt.Digest != digest || receipt.Response != snapshot) {
				t.Fatal("replay rewrote legacy receipt", receipt)
			}
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Write(context.Background(), func(tx *sqlite.Tx) error {
		const digest = "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48"
		key := "legacy-stage"
		if err := tx.SaveReceipt("result", key, digest, snapshot); err != nil {
			return err
		}
		value, err := replay(tx, "result", &key, map[string]any{"value": json.Number("1e0")}, func() (any, error) {
			t.Fatal("legacy stage receipt performed a mutation")
			return nil, nil
		})
		if err != nil {
			return err
		}
		text, err := contract.Canonical(value)
		if err == nil && text != snapshot {
			t.Fatal("stage replay changed captured response", text)
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}
