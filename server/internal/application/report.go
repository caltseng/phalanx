package application

import (
	"context"
	"database/sql"

	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func (a *App) Report(ctx context.Context, itemID, executionID string) (string, error) {
	var result string
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		if err := requireExecution(tx, itemID, executionID); err != nil {
			return err
		}
		status, err := tx.ExecutionStatus(executionID)
		if err != nil {
			return err
		}
		if !Terminal(Status(status)) {
			return &Missing{"Stored report not found"}
		}
		result, err = tx.Report(executionID)
		if err == sql.ErrNoRows {
			return &Missing{"Stored report not found"}
		}
		return err
	})
	return result, err
}
