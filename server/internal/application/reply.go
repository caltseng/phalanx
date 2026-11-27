package application

import (
	"context"
	"errors"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func (a *App) Reply(ctx context.Context, itemID, executionID string, request contract.Reply, key string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "reply:"+itemID+":"+executionID, &key, request, func() (any, error) {
			row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
			if err != nil {
				return nil, err
			}
			waiting, err := tx.HistoryDetails(executionID, row.Version)
			if err != nil {
				return nil, err
			}
			value, _, err := contract.Decode([]byte(waiting))
			if err != nil {
				return nil, err
			}
			details, ok := contract.ObjectValues(value)
			if !ok {
				return nil, errors.New("Input-wait history details are not an object")
			}
			view, err := a.apply(tx, itemID, row, state, ReplyEvent, map[string]any{
				"answer": request.Answer, "answered_by": request.AnsweredBy,
				"wait_version": row.Version, "question": details["question"],
			}, DecisionOptions{}, nil, nil)
			if err != nil {
				return nil, err
			}
			return responseValue(view)
		})
		return err
	})
	return result, err
}
