package application

import (
	"context"
	"database/sql"
	"strconv"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func requireItem(tx *sqlite.Tx, itemID string) error {
	_, err := tx.Item(itemID)
	if err == sql.ErrNoRows {
		return &Missing{"Work item not found"}
	}
	return err
}

func requireExecution(tx *sqlite.Tx, itemID, executionID string) error {
	_, err := executionRow(tx, itemID, executionID)
	return err
}

func executionRow(tx *sqlite.Tx, itemID, executionID string) (sqlite.Execution, error) {
	row, err := tx.Execution(itemID, executionID)
	if err == sql.ErrNoRows {
		return row, &Missing{"Execution not found"}
	}
	return row, err
}

func (a *App) History(ctx context.Context, itemID string) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		if err := requireItem(tx, itemID); err != nil {
			return err
		}
		history, err := tx.Executions(itemID)
		if err != nil {
			return err
		}
		result, err = responseValue(history)
		return err
	})
	return result, err
}

func (a *App) Timeline(ctx context.Context, itemID, executionID string) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		if err := requireExecution(tx, itemID, executionID); err != nil {
			return err
		}
		events, err := tx.Timeline(executionID)
		if err != nil {
			return err
		}
		result, err = responseValue(events)
		return err
	})
	return result, err
}

func (a *App) Draft(ctx context.Context, itemID, executionID, revision string) (string, error) {
	var result string
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		if err := requireExecution(tx, itemID, executionID); err != nil {
			return err
		}
		var err error
		result, err = tx.Draft(executionID, revision)
		if err == sql.ErrNoRows {
			return &Missing{"Report draft not found"}
		}
		return err
	})
	return result, err
}

func (a *App) DisplayTitle(ctx context.Context, itemID string) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = displayTitle(tx, itemID)
		return err
	})
	return result, err
}

func displayTitle(tx *sqlite.Tx, itemID string) (any, error) {
	if err := requireItem(tx, itemID); err != nil {
		return nil, err
	}
	title, err := tx.Title(itemID)
	if err != nil {
		return nil, err
	}
	return responseValue(title)
}

func (a *App) SetDisplayTitle(ctx context.Context, itemID string, request contract.DisplayTitle, key string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "display-title:"+itemID, &key, request, func() (any, error) {
			if err := requireItem(tx, itemID); err != nil {
				return nil, err
			}
			current, err := tx.Title(itemID)
			if err != nil {
				return nil, err
			}
			if strconv.FormatInt(current.Revision, 10) != string(request.ExpectedRevision) {
				return nil, &Conflict{"Display title revision has changed"}
			}
			if err := tx.InsertTitle(itemID, current.Revision+1, request.Title, timestamp(a.Clock())); err != nil {
				return nil, err
			}
			return displayTitle(tx, itemID)
		})
		return err
	})
	return result, err
}
