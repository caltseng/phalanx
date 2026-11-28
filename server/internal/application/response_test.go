package application

import (
	"context"
	"encoding/json"
	"errors"
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

func TestNativeDecodedRequestsReplayHistoricalNumericAndUnicodeDigests(t *testing.T) {
	store, _ := dependencyStore(t)
	const snapshot = `{"id":"historical-receipt"}`
	// Digests are independently captured from Python's historical canonical
	// encoder, not calculated by the implementation under test.
	for _, test := range []struct{ body, digest string }{
		{`{"value":1e0}`, "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48"},
		{`{"value":1.0}`, "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48"},
		{`{"value":1E+0}`, "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48"},
		{`{"unicode":"\u00e9 \ud83d\ude80 <>&","numbers":[-0,-0.0,1e0,0.00001,999999999999999999999999999999,1.234567890123456789]}`,
			"c534d1ac5159674cfa9dc39b8749b526b81c79bc8b6c878144abad9b882f7f03"},
	} {
		t.Run(test.body, func(t *testing.T) {
			decoded, _, err := contract.Decode([]byte(test.body))
			if err != nil {
				t.Fatal(err)
			}
			if err := store.Write(context.Background(), func(tx *sqlite.Tx) error {
				key := test.body
				if err := tx.SaveReceipt("result", key, test.digest, snapshot); err != nil {
					return err
				}
				result, err := replay(tx, "result", &key, decoded, func() (any, error) {
					t.Fatal("historical replay attempted a mutation")
					return nil, nil
				})
				if err != nil {
					return err
				}
				if text, err := contract.Canonical(result); err != nil || text != snapshot {
					t.Fatal(text, err)
				}
				if test.digest == "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48" {
					integer, _, err := contract.Decode([]byte(`{"value":1}`))
					if err != nil {
						return err
					}
					_, err = replay(tx, "result", &key, integer, func() (any, error) {
						t.Fatal("changed numeric kind attempted a mutation")
						return nil, nil
					})
					var conflict *Conflict
					if !errors.As(err, &conflict) {
						t.Fatal("integer and real receipts were conflated", err)
					}
				}
				return nil
			}); err != nil {
				t.Fatal(err)
			}
		})
	}
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
