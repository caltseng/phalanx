package application

import (
	"context"
	"encoding/json"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func (a *App) Inbox(ctx context.Context, query contract.InboxQuery) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		entries, err := tx.Inbox(query.Limit, query.AfterTime, query.AfterID)
		if err != nil {
			return err
		}
		result, err = encoded(entries)
		return err
	})
	return result, err
}
