package application

import (
	"context"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func (a *App) Inbox(ctx context.Context, query contract.InboxQuery) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		entries, err := tx.Inbox(query.Limit, query.AfterTime, query.AfterID)
		if err != nil {
			return err
		}
		result, err = responseValue(entries)
		return err
	})
	return result, err
}
