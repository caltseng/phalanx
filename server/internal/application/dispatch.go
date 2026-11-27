package application

import (
	"context"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func (a *App) DispatchSettings(ctx context.Context) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		settings, err := tx.DispatchSettings()
		if err != nil {
			return err
		}
		result, err = responseValue(settings)
		return err
	})
	return result, err
}

func (a *App) SetDispatch(ctx context.Context, request contract.DispatchUpdate) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		current, err := tx.DispatchSettings()
		if err != nil {
			return err
		}
		var total, building *int64
		if request.MaxWriters != nil {
			value, err := request.MaxWriters.Int64()
			if err != nil {
				return err
			}
			total = &value
		}
		if request.MaxBuildingWriters != nil {
			value, err := request.MaxBuildingWriters.Int64()
			if err != nil {
				return err
			}
			building = &value
		}
		expectedTotal, expectedBuilding := current.MaxWriters, current.MaxBuildingWriters
		if total != nil {
			expectedTotal = *total
		}
		if building != nil {
			expectedBuilding = *building
		}
		if expectedBuilding > expectedTotal {
			return &Conflict{"Building writers must be less than or equal to Total writers; update both limits together"}
		}
		if err := tx.UpdateDispatch(request.Paused, total, building); err != nil {
			return err
		}
		current, err = tx.DispatchSettings()
		if err != nil {
			return err
		}
		result, err = responseValue(current)
		return err
	})
	return result, err
}
