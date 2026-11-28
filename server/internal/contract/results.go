package contract

import (
	"encoding/json"
	"math/big"
)

const PullURLPattern = `^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,9}$`

var stages = []string{"development", "self_review", "report_drafting", "report_review", "report_storage", "publication", "delivery"}

type WaitInput struct {
	Reason  string `json:"reason"`
	Trigger string `json:"trigger"`
}

type Question struct {
	Prompt          string   `json:"prompt"`
	Options         []string `json:"options"`
	ProposedDefault *string  `json:"proposed_default"`
	Handoff         string   `json:"handoff"`
}

type WriterResult struct {
	ExpectedVersion   json.Number  `json:"expected_version"`
	AttemptID         *string      `json:"attempt_id"`
	Stage             string       `json:"stage"`
	Outcome           string       `json:"outcome"`
	RetryAfterSeconds *json.Number `json:"retry_after_seconds"`
	Wait              *WaitInput   `json:"wait"`
	Question          *Question    `json:"question"`
	FailureReason     *string      `json:"failure_reason"`
	Candidate         *string      `json:"candidate"`
	ReportMarkdown    *string      `json:"report_markdown"`
	ReportRevision    *string      `json:"report_revision"`
	ReportReviewNotes *string      `json:"report_review_notes"`
	ReviewedCandidate *string      `json:"reviewed_candidate"`
	ReviewNotes       *string      `json:"review_notes"`
	PRURL             *string      `json:"pr_url"`
}

type AttemptEvent struct {
	ExpectedVersion json.Number `json:"expected_version"`
	AttemptID       string      `json:"attempt_id"`
	Stage           string      `json:"stage"`
}

func (v *validator) optionalText(body map[string]any, name string, prefix []any) *string {
	if value, exists := body[name]; exists && value != nil {
		text := v.text(value, location(prefix, name), true, 1, 16000, "")
		return &text
	}
	return nil
}

func (v *validator) waitInput(value any, prefix []any) *WaitInput {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(prefix)...)
		return nil
	}
	result := &WaitInput{}
	if x, ok := v.required(body, "reason", prefix); ok {
		result.Reason = v.text(x, location(prefix, "reason"), true, 1, 16000, "")
	}
	if x, ok := v.required(body, "trigger", prefix); ok {
		result.Trigger = v.text(x, location(prefix, "trigger"), true, 1, 16000, "")
	}
	v.extras(ObjectKeys(value), []string{"reason", "trigger"}, prefix)
	return result
}

func (v *validator) question(value any, prefix []any) *Question {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(prefix)...)
		return nil
	}
	result := &Question{Options: []string{}}
	if x, ok := v.required(body, "prompt", prefix); ok {
		result.Prompt = v.text(x, location(prefix, "prompt"), true, 1, 16000, "")
	}
	if x, ok := body["options"]; ok {
		result.Options = v.texts(x, location(prefix, "options"), 20)
	}
	result.ProposedDefault = v.optionalText(body, "proposed_default", prefix)
	if x, ok := v.required(body, "handoff", prefix); ok {
		result.Handoff = v.text(x, location(prefix, "handoff"), true, 1, 16000, "")
	}
	v.extras(ObjectKeys(value), []string{"prompt", "options", "proposed_default", "handoff"}, prefix)
	return result
}

func ValidateWriterResult(value any, keys []string) (WriterResult, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return WriterResult{}, modelIssue(prefix)
	}
	v, result := validator{}, WriterResult{}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	if x, ok := body["attempt_id"]; ok && x != nil {
		text := v.text(x, location(prefix, "attempt_id"), false, 0, 0, IdentifierPattern)
		result.AttemptID = &text
	}
	if x, ok := v.required(body, "stage", prefix); ok {
		result.Stage = v.choice(x, location(prefix, "stage"), stages)
	}
	if x, ok := v.required(body, "outcome", prefix); ok {
		result.Outcome = v.choice(x, location(prefix, "outcome"), []string{"succeeded", "changes_required", "needs_input", "failed"})
	}
	if x, ok := body["retry_after_seconds"]; ok && x != nil {
		at := location(prefix, "retry_after_seconds")
		number := v.strictInteger(x, at, 0)
		integer, _ := new(big.Int).SetString(string(number), 10)
		if integer.Cmp(big.NewInt(1000000000000)) > 0 {
			v.issue("Input should be less than or equal to 1000000000000", at)
		}
		result.RetryAfterSeconds = &number
	}
	if x, ok := body["wait"]; ok && x != nil {
		result.Wait = v.waitInput(x, location(prefix, "wait"))
	}
	if x, ok := body["question"]; ok && x != nil {
		result.Question = v.question(x, location(prefix, "question"))
	}
	result.FailureReason = v.optionalText(body, "failure_reason", prefix)
	result.Candidate = v.optionalText(body, "candidate", prefix)
	if x, ok := body["report_markdown"]; ok && x != nil {
		text := v.text(x, location(prefix, "report_markdown"), false, 1, 256000, `\S`)
		result.ReportMarkdown = &text
	}
	if x, ok := body["report_revision"]; ok && x != nil {
		text := v.text(x, location(prefix, "report_revision"), false, 0, 0, RevisionPattern)
		result.ReportRevision = &text
	}
	result.ReportReviewNotes = v.optionalText(body, "report_review_notes", prefix)
	result.ReviewedCandidate = v.optionalText(body, "reviewed_candidate", prefix)
	result.ReviewNotes = v.optionalText(body, "review_notes", prefix)
	if x, ok := body["pr_url"]; ok && x != nil {
		text := v.text(x, location(prefix, "pr_url"), false, 0, 0, PullURLPattern)
		result.PRURL = &text
	}
	v.extras(keys, []string{"expected_version", "attempt_id", "stage", "outcome", "retry_after_seconds", "wait", "question",
		"failure_reason", "candidate", "report_markdown", "report_revision", "report_review_notes", "reviewed_candidate", "review_notes", "pr_url"}, prefix)
	if len(v.issues) == 0 && result.AttemptID == nil &&
		!(result.Stage == "delivery" && result.Outcome == "needs_input" && result.Question != nil) {
		v.issue("Value error, An attempt is required except for a delivery question", prefix)
	}
	return result, v.issues
}

func ValidateAttemptEvent(value any, keys []string) (AttemptEvent, []Issue) {
	body, ok := object(value)
	prefix := []any{"body"}
	if !ok {
		return AttemptEvent{}, modelIssue(prefix)
	}
	v, result := validator{}, AttemptEvent{}
	if x, ok := v.required(body, "expected_version", prefix); ok {
		result.ExpectedVersion = v.strictInteger(x, location(prefix, "expected_version"), 1)
	}
	if x, ok := v.required(body, "attempt_id", prefix); ok {
		result.AttemptID = v.text(x, location(prefix, "attempt_id"), false, 0, 0, IdentifierPattern)
	}
	if x, ok := v.required(body, "stage", prefix); ok {
		result.Stage = v.choice(x, location(prefix, "stage"), stages)
	}
	v.extras(keys, []string{"expected_version", "attempt_id", "stage"}, prefix)
	return result, v.issues
}
