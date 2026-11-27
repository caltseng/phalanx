package application

import (
	"context"
	"strings"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type ArchiveStatusView struct {
	Record  *sqlite.Archive        `json:"record"`
	Writers []sqlite.ArchiveWriter `json:"writers"`
	Active  bool                   `json:"active"`
}

func archiveStatus(tx *sqlite.Tx, sessionID string) (ArchiveStatusView, error) {
	var result ArchiveStatusView
	record, err := tx.Archive(sessionID)
	if err != nil {
		return result, err
	}
	writers, err := tx.ArchiveWriters(sessionID)
	if err != nil {
		return result, err
	}
	active := false
	for _, writer := range writers {
		active = active || writer.ItemActive
	}
	captains, err := tx.AllCaptains()
	if err != nil {
		return result, err
	}
	ids := []string{}
	matching := []sqlite.Captain{}
	for _, captain := range captains {
		if sessionIdentity(captain.SessionRef) != sessionID {
			continue
		}
		ids = append(ids, captain.ID)
		matching = append(matching, captain)
	}
	details, err := tx.CaptainDetailsBatch(ids)
	if err != nil {
		return result, err
	}
	for _, captain := range matching {
		detail := details[captain.ID]
		clean := captain.HandoffNote != nil && strings.HasPrefix(*captain.HandoffNote, "Stop condition met:")
		if !clean {
			clean = len(detail.Successors) > 0
		}
		active = active || captain.State != "ended" || len(detail.OpenIntents) > 0 || detail.CleanupPending > 0 || !clean
	}
	return ArchiveStatusView{Record: record, Writers: writers, Active: active}, nil
}

func (a *App) ArchiveStatus(ctx context.Context, sessionID string) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		status, err := archiveStatus(tx, sessionID)
		if err != nil {
			return err
		}
		result, err = responseValue(status)
		return err
	})
	return result, err
}

func (a *App) RecordArchive(ctx context.Context, request contract.SessionArchive) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		status, err := archiveStatus(tx, request.SessionID)
		if err != nil {
			return err
		}
		if status.Active && request.Outcome == "archived" {
			return &Conflict{"The session still owns a writer or an unfinished Captain handoff"}
		}
		previous := status.Record
		if previous != nil && (previous.Outcome == "archived" || previous.Outcome == "refused" && request.Outcome != "archived") {
			result, err = responseValue(previous)
			return err
		}
		if err := tx.RecordArchive(request.SessionID, request.Outcome, request.Reason, request.ReportedBy, timestamp(a.Clock())); err != nil {
			return err
		}
		updated, err := tx.Archive(request.SessionID)
		if err != nil {
			return err
		}
		result, err = responseValue(updated)
		return err
	})
	return result, err
}
