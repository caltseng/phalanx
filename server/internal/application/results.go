package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func newWait(reason, trigger string) (*Wait, error) {
	space := func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f }
	if strings.TrimFunc(reason, space) == "" || strings.TrimFunc(trigger, space) == "" {
		return nil, errors.New("A wait requires both a reason and a resume trigger")
	}
	return &Wait{reason, trigger}, nil
}

var githubRemote = regexp.MustCompile(`(?i)^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?$`)

func githubRepository(remote string) *string {
	for _, r := range remote {
		if r > 127 {
			return nil
		}
	}
	match := githubRemote.FindStringSubmatch(remote)
	if match == nil {
		return nil
	}
	return &match[1]
}

func checkAttempt(tx *sqlite.Tx, writerID, executionID string, state State, attemptID, stage string) error {
	current, err := tx.CurrentAttemptStage(writerID, executionID, attemptID)
	if err == sql.ErrNoRows || (err == nil && (stage != string(state.Stage) || current != string(state.Stage))) {
		return &Conflict{"Writer, attempt, or stage is no longer current"}
	}
	return err
}

func stalledRework(tx *sqlite.Tx, executionID string, next State) (map[string]any, error) {
	total, repeated, err := tx.ReworkCounts(executionID, *next.Findings)
	if err != nil {
		return nil, err
	}
	reason := ""
	if repeated >= 2 {
		reason = "The same findings returned after 2 revision rounds"
	} else if total >= 10 {
		reason = "The PR needed 10 revision rounds without becoming ready"
	}
	if reason == "" {
		return nil, nil
	}
	text := func(value *string) string {
		if value == nil {
			return "None"
		}
		return *value
	}
	handoff := []rune(fmt.Sprintf("%s, candidate %s\n\nLatest findings:\n%s",
		text(next.PrimaryPR), text(next.Candidate), *next.Findings))
	if len(handoff) > 16000 {
		handoff = handoff[:16000]
	}
	return map[string]any{
		"prompt":  reason + ". Resolve the PR or give direction, then reply to retry.",
		"handoff": string(handoff),
	}, nil
}

func (a *App) result(tx *sqlite.Tx, itemID, executionID, writerID string, request contract.WriterResult) (sqlite.Execution, error) {
	row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
	if err != nil {
		return sqlite.Execution{}, err
	}
	if request.AttemptID == nil {
		writer, err := boundWriter(tx, itemID, executionID, writerID)
		if err != nil {
			return sqlite.Execution{}, err
		}
		active, err := tx.OpenAttempt(executionID)
		if err != nil {
			return sqlite.Execution{}, err
		}
		if writer.EndedAt != nil || writer.AttemptID != nil || active != nil || state.Stage != Delivery ||
			request.Stage != string(state.Stage) || state.Status != WaitingForExternal ||
			request.Outcome != "needs_input" || request.Question == nil {
			return sqlite.Execution{}, &Conflict{"An attempt-free question requires this writer's delivery external wait"}
		}
	} else if err := checkAttempt(tx, writerID, executionID, state, *request.AttemptID, request.Stage); err != nil {
		return sqlite.Execution{}, err
	}
	reviewed := request.Candidate
	if request.Outcome == "succeeded" {
		reviewed = request.ReviewedCandidate
	}
	if request.ReviewNotes != nil && (state.Stage != SelfReview || state.Candidate == nil || !sameText(state.Candidate, reviewed)) {
		return sqlite.Execution{}, &Conflict{"Review notes require self-review evidence for the current candidate"}
	}
	var wait *Wait
	if request.Wait != nil {
		wait, err = newWait(request.Wait.Reason, request.Wait.Trigger)
		if err != nil {
			return sqlite.Execution{}, err
		}
	}
	if request.Question != nil {
		reason := request.Question.Prompt
		if wait != nil {
			reason = wait.Reason
		}
		wait, err = newWait(reason, "owner reply")
		if err != nil {
			return sqlite.Execution{}, err
		}
	}
	payload, err := encoded(request)
	if err != nil {
		return sqlite.Execution{}, err
	}
	value, _, err := contract.Decode(payload)
	if err != nil {
		return sqlite.Execution{}, err
	}
	details, ok := contract.ObjectValues(value)
	if !ok {
		return sqlite.Execution{}, errors.New("Writer result is not an object")
	}
	delete(details, "report_markdown")
	delete(details, "expected_version")
	if request.ReviewNotes == nil {
		delete(details, "review_notes")
	}
	if request.PRURL == nil {
		delete(details, "pr_url")
	}
	if request.AttemptID == nil {
		details["writer_id"] = writerID
	}
	publishing := state.Stage == Publication && request.Outcome == "succeeded"
	if (request.PRURL != nil) != publishing {
		return sqlite.Execution{}, &Conflict{"A successful publication result reports the PR URL, and only it does"}
	}
	var published *string
	if publishing {
		item, err := tx.Item(itemID)
		if err != nil {
			return sqlite.Execution{}, err
		}
		repo, err := tx.Repository(item.RepositoryID)
		if err != nil {
			return sqlite.Execution{}, err
		}
		name := githubRepository(repo.Remote)
		if name == nil || !strings.HasPrefix(strings.ToLower(*request.PRURL), "https://github.com/"+strings.ToLower(*name)+"/pull/") {
			return sqlite.Execution{}, &Conflict{"The PR must be in the item's registered GitHub repository"}
		}
		text := "https://github.com/" + *name + "/pull/" + (*request.PRURL)[strings.LastIndexByte(*request.PRURL, '/')+1:]
		published = &text
		previous, err := tx.PreviousMergedPR(itemID, executionID, text)
		if err != nil {
			return sqlite.Execution{}, err
		}
		if previous {
			return sqlite.Execution{}, &Conflict{text + " was already merged for an earlier execution. Publish this execution's work in a new PR"}
		}
	}
	var retryAt *int64
	if request.RetryAfterSeconds != nil && request.FailureReason == nil {
		return sqlite.Execution{}, &Conflict{"A retry delay requires a technical failure reason"}
	}
	if request.FailureReason != nil {
		if request.Outcome != "failed" || wait != nil {
			return sqlite.Execution{}, &Conflict{"A technical failure reason requires a failed result without an explicit wait"}
		}
		if row.RetryCount >= 4 {
			attempts := "stopped worker attempts"
			if request.RetryAfterSeconds != nil {
				attempts = "technical failures"
			}
			wait, err = newWait("Five "+attempts+" require a recovery decision.\n\n"+*request.FailureReason, "owner intervention")
			if err != nil {
				return sqlite.Execution{}, err
			}
		} else {
			now := time.Now().UnixMilli()
			at := now + (5<<row.RetryCount)*1000
			if request.RetryAfterSeconds != nil {
				delay, err := request.RetryAfterSeconds.Int64()
				if err != nil {
					return sqlite.Execution{}, err
				}
				at = max(at, now+delay*1000)
			}
			retryAt = &at
			details["retry_at_ms"] = at
		}
	}
	if request.ReportReviewNotes != nil && (state.Stage != ReportReview ||
		(request.Outcome != "succeeded" && request.Outcome != "changes_required")) {
		return sqlite.Execution{}, &Conflict{"Review notes require a successful review or changes-required result"}
	}
	draft := request.ReportRevision
	if request.ReportMarkdown != nil {
		if state.Stage != ReportDrafting || request.Outcome != "succeeded" {
			return sqlite.Execution{}, &Conflict{"Markdown is accepted on a successful report drafting result"}
		}
		if draft != nil {
			return sqlite.Execution{}, &Conflict{"The server derives the revision from the draft Markdown"}
		}
		revision := contract.Digest(*request.ReportMarkdown)
		draft = &revision
		if err := tx.InsertDraft(executionID, revision, *request.ReportMarkdown); err != nil {
			return sqlite.Execution{}, err
		}
		details["report_revision"] = revision
	} else if state.Stage == ReportDrafting && request.Outcome == "succeeded" {
		return sqlite.Execution{}, &Conflict{"Successful drafting requires the draft Markdown"}
	}
	if state.Stage == ReportStorage && request.Outcome == "succeeded" {
		if draft == nil || !sameText(draft, state.ReportDraft) {
			return sqlite.Execution{}, &Conflict{"Storage requires the current reviewed draft revision"}
		}
		exists, err := tx.DraftExists(executionID, *draft)
		if err != nil {
			return sqlite.Execution{}, err
		}
		if !exists {
			return sqlite.Execution{}, &Conflict{"Reviewed draft Markdown is unavailable"}
		}
	}
	var findings *string
	if request.Outcome == "changes_required" {
		findings = request.ReviewNotes
	}
	return a.apply(tx, itemID, row, state, Result, details, DecisionOptions{
		Outcome: Outcome(request.Outcome), Wait: wait, RetryAllowed: retryAt != nil,
		Evidence: Evidence{Candidate: request.Candidate, ReviewedCandidate: request.ReviewedCandidate,
			ReportDraft: draft, ReviewedReport: draft, ReportReviewNotes: request.ReportReviewNotes,
			Findings: findings, PublishedPR: published},
	}, request.AttemptID, retryAt)
}

func (a *App) advanceWriter(tx *sqlite.Tx, writerID string, previousAttempt *string, view sqlite.Execution) (json.RawMessage, error) {
	writer, err := writerRecord(tx, writerID)
	if err != nil {
		return nil, err
	}
	if writer.EndedAt == nil {
		state, err := readState(view.State)
		if err != nil {
			return nil, err
		}
		if Terminal(state.Status) {
			if err := tx.EndWriters(writer.ExecutionID, string(state.Status), timestamp(a.Clock())); err != nil {
				return nil, err
			}
		} else {
			var previous string
			if previousAttempt != nil {
				previous, err = tx.AttemptStage(*previousAttempt)
				if err != nil && err != sql.ErrNoRows {
					return nil, err
				}
			}
			if previous == "self_review" && state.Stage == Publication {
				if err := tx.RelinquishBuilding(writerID); err != nil {
					return nil, err
				}
				if err := tx.SetHandoff(writer.ExecutionID, nil); err != nil {
					return nil, err
				}
				writer, err = writerRecord(tx, writerID)
				if err != nil {
					return nil, err
				}
			}
			if err := a.claimNext(tx, writer, previousAttempt); err != nil {
				return nil, err
			}
		}
	}
	writer, err = writerRecord(tx, writerID)
	if err != nil {
		return nil, err
	}
	return binding(tx, writer)
}

func (a *App) WriterResult(ctx context.Context, itemID, executionID, writerID string, request contract.WriterResult, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "result:"+itemID+":"+executionID, key,
			map[string]any{"body": request, "writer_id": writerID}, func() (json.RawMessage, error) {
				view, err := a.result(tx, itemID, executionID, writerID, request)
				if err != nil {
					return nil, err
				}
				return a.advanceWriter(tx, writerID, request.AttemptID, view)
			})
		return err
	})
	if err != nil {
		return nil, err
	}
	return a.finishResponse(ctx, result)
}

func (a *App) WriterStopConfirmed(ctx context.Context, itemID, executionID, writerID string, request contract.AttemptEvent, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "stop_confirmed:"+itemID+":"+executionID, key,
			map[string]any{"body": request, "writer_id": writerID}, func() (json.RawMessage, error) {
				row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
				if err != nil {
					return nil, err
				}
				if err := checkAttempt(tx, writerID, executionID, state, request.AttemptID, request.Stage); err != nil {
					return nil, err
				}
				if err := a.confirmStopped(tx, row, state, &request.AttemptID, map[string]any{}); err != nil {
					return nil, err
				}
				view, err := tx.Execution(itemID, executionID)
				if err != nil {
					return nil, err
				}
				return a.advanceWriter(tx, writerID, &request.AttemptID, view)
			})
		return err
	})
	if err != nil {
		return nil, err
	}
	return a.finishResponse(ctx, result)
}
