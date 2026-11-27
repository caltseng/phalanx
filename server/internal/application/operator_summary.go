package application

import "encoding/json"

type OperatorSummary struct {
	ID                       string            `json:"id"`
	ItemID                   string            `json:"item_id"`
	Version                  int64             `json:"version"`
	Title                    string            `json:"title"`
	Goal                     *string           `json:"goal"`
	Priority                 string            `json:"priority"`
	Label                    *string           `json:"label"`
	Target                   Target            `json:"target"`
	TargetBranch             *string           `json:"target_branch"`
	PublicationMode          *PublicationMode  `json:"publication_mode"`
	RepositoryID             string            `json:"repository_id"`
	CreatedAt                string            `json:"created_at"`
	ActiveStartedAt          *string           `json:"active_started_at"`
	ActiveAgeSeconds         *int64            `json:"active_age_seconds"`
	StageStartedAt           *string           `json:"stage_started_at"`
	Stage                    string            `json:"stage"`
	Status                   string            `json:"status"`
	RevisionRound            int64             `json:"revision_round"`
	LastAdvancedAt           string            `json:"last_advanced_at"`
	LastProgressAt           string            `json:"last_progress_at"`
	WaitReason               *string           `json:"wait_reason"`
	Pickup                   *PickupDecision   `json:"pickup"`
	BlockedPrerequisiteCount int64             `json:"blocked_prerequisite_count"`
	ResumeTrigger            *string           `json:"resume_trigger"`
	NextAction               *string           `json:"next_action"`
	RetryAtMS                *int64            `json:"retry_at_ms"`
	StageOwner               *string           `json:"stage_owner"`
	WaitSince                *string           `json:"wait_since"`
	WaitAgeSeconds           *int64            `json:"wait_age_seconds"`
	PrimaryPRURL             *string           `json:"primary_pr_url"`
	PR                       *operatorPR       `json:"pr"`
	DuplicateCount           any               `json:"duplicate_count"`
	ClosedAt                 *string           `json:"closed_at"`
	Outcome                  *string           `json:"outcome"`
	Output                   *operatorOutput   `json:"output"`
	OutputRevision           *Revision         `json:"output_revision"`
	Delivery                 *operatorDelivery `json:"delivery"`
	Deployment               json.RawMessage   `json:"deployment"`
	Writer                   *operatorWriter   `json:"writer"`
	*OperatorBrief
}

type OperatorBrief struct {
	AcceptanceCriteria any `json:"acceptance_criteria"`
	ContextLinks       any `json:"context_links"`
}

type operatorPR struct {
	URL       *string `json:"url"`
	Number    any     `json:"number"`
	State     *string `json:"state"`
	Draft     *bool   `json:"draft"`
	HeadSHA   *string `json:"head_sha"`
	HeadCI    *string `json:"head_ci"`
	Additions any     `json:"additions"`
	Deletions any     `json:"deletions"`
}

type operatorOutput struct {
	Kind string `json:"kind"`
	URL  string `json:"url"`
}

type operatorDelivery struct {
	Merged      bool    `json:"merged"`
	MergeCommit *string `json:"merge_commit"`
	MergeMethod *string `json:"merge_method"`
	MergedAt    *string `json:"merged_at"`
}

type operatorWriter struct {
	ID                  *string     `json:"id"`
	State               *string     `json:"state"`
	Runtime             *string     `json:"runtime"`
	EndedAt             *string     `json:"ended_at"`
	CaptainID           *string     `json:"captain_id"`
	SessionID           *string     `json:"session_id"`
	SessionURL          *string     `json:"session_url"`
	LastProgressAt      *string     `json:"last_progress_at"`
	SessionLink         SessionLink `json:"session_link"`
	ProgressNote        *string     `json:"progress_note"`
	Nonce               *string     `json:"nonce"`
	CreatedAt           *string     `json:"created_at"`
	ConfirmedAt         *string     `json:"confirmed_at"`
	AbandonedAt         *string     `json:"abandoned_at"`
	Building            bool        `json:"building"`
	BuildingRequestedAt *string     `json:"building_requested_at"`
	SessionAvailability string      `json:"session_availability"`
}
