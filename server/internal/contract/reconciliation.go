package contract

import "encoding/json"

type ObservedPull struct {
	Number             json.Number  `json:"number"`
	State              string       `json:"state"`
	Merged             bool         `json:"merged"`
	Draft              bool         `json:"draft"`
	HeadRef            string       `json:"head_ref"`
	HeadSHA            string       `json:"head_sha"`
	BaseRef            string       `json:"base_ref"`
	BaseSHA            string       `json:"base_sha"`
	MergeCommit        *string      `json:"merge_commit"`
	Mergeable          *bool        `json:"mergeable"`
	MergeMethod        *string      `json:"merge_method"`
	MergedAt           *Timestamp   `json:"merged_at"`
	MergedHeadAncestor *string      `json:"merged_head_ancestor"`
	Additions          *json.Number `json:"additions"`
	Deletions          *json.Number `json:"deletions"`
}

type Deployment struct {
	Commit    string `json:"commit"`
	CheckName string `json:"check_name"`
	State     string `json:"state"`
}

type Observation struct {
	ObservedAt     Timestamp     `json:"observed_at"`
	PR             *ObservedPull `json:"pr"`
	Checks         *string       `json:"checks"`
	CheckReasons   []string      `json:"check_reasons"`
	Feedback       []string      `json:"feedback"`
	MergedBranch   bool          `json:"merged_branch"`
	TargetBase     *string       `json:"target_base"`
	Deployment     *Deployment   `json:"deployment"`
	DuplicateCount *json.Number  `json:"duplicate_count"`
}

type LocalChecks struct {
	Head     string   `json:"head"`
	Failures []string `json:"failures"`
}

type ReconcilerHeartbeat struct {
	DeploymentChecks map[string]*string `json:"deployment_checks"`
	DeploymentOrder  []string           `json:"-"`
}

func (v *validator) optionalPattern(body map[string]any, field string, at []any, pattern string) *string {
	if value, exists := body[field]; exists && value != nil {
		text := v.text(value, location(at, field), false, 0, 0, pattern)
		return &text
	}
	return nil
}

func (v *validator) optionalCount(body map[string]any, field string, at []any) *json.Number {
	if value, exists := body[field]; exists && value != nil {
		number := v.strictInteger(value, location(at, field), 0)
		return &number
	}
	return nil
}

func (v *validator) valueError(at []any, message string) {
	v.issue("Value error, "+message, at)
}

func (v *validator) observedPull(value any, at []any) *ObservedPull {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return nil
	}
	start := len(v.issues)
	result := &ObservedPull{}
	if value, exists := v.required(body, "number", at); exists {
		result.Number = v.strictInteger(value, location(at, "number"), 1)
	}
	if value, exists := v.required(body, "state", at); exists {
		result.State = v.choice(value, location(at, "state"), []string{"open", "closed"})
	}
	if value, exists := v.required(body, "merged", at); exists {
		result.Merged = v.boolean(value, location(at, "merged"), false)
	}
	if value, exists := v.required(body, "draft", at); exists {
		result.Draft = v.boolean(value, location(at, "draft"), false)
	}
	for _, field := range []string{"head_ref", "head_sha", "base_ref", "base_sha"} {
		if value, exists := v.required(body, field, at); exists {
			var text string
			if field == "head_ref" || field == "base_ref" {
				text = v.text(value, location(at, field), false, 1, 255, "")
			} else {
				text = v.text(value, location(at, field), false, 0, 0, SHAPattern)
			}
			switch field {
			case "head_ref":
				result.HeadRef = text
			case "head_sha":
				result.HeadSHA = text
			case "base_ref":
				result.BaseRef = text
			case "base_sha":
				result.BaseSHA = text
			}
		}
	}
	result.MergeCommit = v.optionalPattern(body, "merge_commit", at, SHAPattern)
	if value, exists := body["mergeable"]; exists && value != nil {
		flag := v.boolean(value, location(at, "mergeable"), false)
		result.Mergeable = &flag
	}
	if value, exists := body["merge_method"]; exists && value != nil {
		text := v.choice(value, location(at, "merge_method"), []string{"merge", "squash_or_rebase"})
		result.MergeMethod = &text
	}
	if value, exists := body["merged_at"]; exists && value != nil {
		stamp := v.timestamp(value, location(at, "merged_at"))
		result.MergedAt = &stamp
	}
	result.MergedHeadAncestor = v.optionalPattern(body, "merged_head_ancestor", at, SHAPattern)
	result.Additions = v.optionalCount(body, "additions", at)
	result.Deletions = v.optionalCount(body, "deletions", at)
	v.extras(ObjectKeys(value), []string{"number", "state", "merged", "draft", "head_ref", "head_sha", "base_ref",
		"base_sha", "merge_commit", "mergeable", "merge_method", "merged_at", "merged_head_ancestor", "additions", "deletions"}, at)
	if len(v.issues) == start && !result.Merged && (result.MergeMethod != nil || result.MergedAt != nil || result.MergedHeadAncestor != nil) {
		v.valueError(at, "Merge details describe a merged PR")
	}
	return result
}

func (v *validator) reconciliationReasons(value any, at []any, maximum int) []string {
	result := []string{}
	values, ok := value.([]any)
	if !ok {
		v.issue("Input should be a valid list", at)
		return result
	}
	start := len(v.issues)
	for index, entry := range values {
		result = append(result, v.text(entry, location(at, index), true, 1, 4000, ""))
	}
	if len(v.issues) == start && len(values) > maximum {
		v.tooLong(at, maximum, len(values))
	}
	return result
}

func (v *validator) deployment(value any, at []any) *Deployment {
	body, ok := object(value)
	if !ok {
		v.issues = append(v.issues, modelIssue(at)...)
		return nil
	}
	result := &Deployment{}
	if value, exists := v.required(body, "commit", at); exists {
		result.Commit = v.text(value, location(at, "commit"), false, 0, 0, SHAPattern)
	}
	if value, exists := v.required(body, "check_name", at); exists {
		result.CheckName = v.text(value, location(at, "check_name"), true, 1, 255, "")
	}
	if value, exists := v.required(body, "state", at); exists {
		result.State = v.choice(value, location(at, "state"), []string{"pending", "failed", "deployed", "disabled"})
	}
	v.extras(ObjectKeys(value), []string{"commit", "check_name", "state"}, at)
	return result
}

func ValidateObservation(value any, keys []string) (Observation, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return Observation{}, modelIssue(at)
	}
	v, result := validator{}, Observation{CheckReasons: []string{}, Feedback: []string{}}
	if value, exists := v.required(body, "observed_at", at); exists {
		result.ObservedAt = v.timestamp(value, location(at, "observed_at"))
	}
	if value, exists := body["pr"]; exists && value != nil {
		result.PR = v.observedPull(value, location(at, "pr"))
	}
	if value, exists := body["checks"]; exists && value != nil {
		text := v.choice(value, location(at, "checks"), []string{"satisfied", "not_configured", "waiting", "failed", "blocked"})
		result.Checks = &text
	}
	if value, exists := body["check_reasons"]; exists {
		result.CheckReasons = v.reconciliationReasons(value, location(at, "check_reasons"), 20)
	}
	if value, exists := body["feedback"]; exists {
		result.Feedback = v.reconciliationReasons(value, location(at, "feedback"), 50)
	}
	if value, exists := body["merged_branch"]; exists {
		result.MergedBranch = v.boolean(value, location(at, "merged_branch"), false)
	}
	if value, exists := body["target_base"]; exists && value != nil {
		text := v.text(value, location(at, "target_base"), false, 1, 255, "")
		result.TargetBase = &text
	}
	if value, exists := body["deployment"]; exists && value != nil {
		result.Deployment = v.deployment(value, location(at, "deployment"))
	}
	result.DuplicateCount = v.optionalCount(body, "duplicate_count", at)
	v.extras(keys, []string{"observed_at", "pr", "checks", "check_reasons", "feedback", "merged_branch", "target_base",
		"deployment", "duplicate_count"}, at)
	if len(v.issues) == 0 {
		message := ""
		switch {
		case result.PR == nil && (result.Checks != nil || len(result.CheckReasons) > 0 || len(result.Feedback) > 0):
			message = "Checks and feedback describe a PR"
		case result.PR != nil && result.MergedBranch:
			message = "merged_branch describes an item branch with no PR"
		case (result.PR == nil) != (result.TargetBase == nil):
			message = "A PR observation names the branch it must merge into, and only it does"
		case result.Deployment != nil && (result.PR == nil || !result.PR.Merged ||
			result.PR.BaseRef != *result.TargetBase || result.PR.MergeCommit == nil || *result.PR.MergeCommit != result.Deployment.Commit):
			message = "Deployment describes the merge commit of a PR merged into its target"
		}
		if message != "" {
			v.valueError(at, message)
		}
	}
	return result, v.issues
}

func ValidateLocalChecks(value any, keys []string) (LocalChecks, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return LocalChecks{}, modelIssue(at)
	}
	v, result := validator{}, LocalChecks{Failures: []string{}}
	if value, exists := v.required(body, "head", at); exists {
		result.Head = v.text(value, location(at, "head"), false, 0, 0, SHAPattern)
	}
	if value, exists := body["failures"]; exists {
		result.Failures = v.reconciliationReasons(value, location(at, "failures"), 50)
	}
	v.extras(keys, []string{"head", "failures"}, at)
	return result, v.issues
}

func ValidateReconcilerHeartbeat(value any, keys []string) (ReconcilerHeartbeat, []Issue) {
	body, ok := object(value)
	at := []any{"body"}
	if !ok {
		return ReconcilerHeartbeat{}, modelIssue(at)
	}
	v, result := validator{}, ReconcilerHeartbeat{DeploymentChecks: map[string]*string{}}
	if value, exists := body["deployment_checks"]; exists {
		entries, ok := object(value)
		where := location(at, "deployment_checks")
		if !ok {
			v.issue("Input should be a valid dictionary", where)
		} else {
			result.DeploymentOrder = ObjectKeys(value)
			for _, key := range ObjectKeys(value) {
				v.text(key, location(location(where, key), "[key]"), false, 0, 0, IdentifierPattern)
				var name *string
				if entries[key] != nil {
					text := v.text(entries[key], location(where, key), false, 1, 255, "")
					name = &text
				}
				result.DeploymentChecks[key] = name
			}
		}
	}
	v.extras(keys, []string{"deployment_checks"}, at)
	return result, v.issues
}
