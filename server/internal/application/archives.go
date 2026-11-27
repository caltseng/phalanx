package application

import (
	"context"
	"encoding/json"
	"errors"
	"strings"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func archiveStatus(tx *sqlite.Tx, sessionID string) (map[string]any, error) {
	record, err := tx.Archive(sessionID)
	if err != nil {
		return nil, err
	}
	writers, err := tx.ArchiveWriters(sessionID)
	if err != nil {
		return nil, err
	}
	active := false
	for _, writer := range writers {
		item, ok := writer["item_id"].(string)
		if !ok {
			return nil, errors.New("Archived writer item identity is not text")
		}
		found, err := tx.ItemHasWriter(item)
		if err != nil {
			return nil, err
		}
		active = active || found
	}
	captains, err := tx.AllCaptains()
	if err != nil {
		return nil, err
	}
	for _, captain := range captains {
		if sessionIdentity(captain.SessionRef) != sessionID {
			continue
		}
		details, err := tx.CaptainDetails(captain.ID)
		if err != nil {
			return nil, err
		}
		clean := captain.HandoffNote != nil && strings.HasPrefix(*captain.HandoffNote, "Stop condition met:")
		if !clean {
			clean, err = tx.CaptainHasSuccessor(captain.ID)
			if err != nil {
				return nil, err
			}
		}
		active = active || captain.State != "ended" || len(details.OpenIntents) > 0 || details.CleanupPending > 0 || !clean
	}
	return map[string]any{"record": record, "writers": writers, "active": active}, nil
}

func (a *App) ArchiveStatus(ctx context.Context, sessionID string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		status, err := archiveStatus(tx, sessionID)
		if err != nil {
			return err
		}
		result, err = encoded(status)
		return err
	})
	return result, err
}

func (a *App) RecordArchive(ctx context.Context, request contract.SessionArchive) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		status, err := archiveStatus(tx, request.SessionID)
		if err != nil {
			return err
		}
		active, ok := status["active"].(bool)
		if !ok {
			return errors.New("Archive activity is not a boolean")
		}
		if active && request.Outcome == "archived" {
			return &Conflict{"The session still owns a writer or an unfinished Captain handoff"}
		}
		previous, err := tx.Archive(request.SessionID)
		if err != nil {
			return err
		}
		if previous != nil && (previous.Outcome == "archived" || previous.Outcome == "refused" && request.Outcome != "archived") {
			result, err = encoded(previous)
			return err
		}
		if err := tx.RecordArchive(request.SessionID, request.Outcome, request.Reason, request.ReportedBy, timestamp(a.Clock())); err != nil {
			return err
		}
		updated, err := tx.Archive(request.SessionID)
		if err != nil {
			return err
		}
		result, err = encoded(updated)
		return err
	})
	return result, err
}
