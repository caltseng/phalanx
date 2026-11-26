package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func (h *Handler) captainOperation(ctx context.Context, r *http.Request, target route, input any, keys []string,
	pathIssues []contract.Issue, key *string) (json.RawMessage, []contract.Issue, error) {
	switch target.kind {
	case "captains":
		limit, issues := contract.ValidateLimit(r.URL.Query(), 25)
		if len(issues) > 0 {
			return nil, issues, nil
		}
		result, err := h.App.Captains(ctx, limit)
		return result, nil, err
	case "captain":
		result, err := h.App.Captain(ctx, target.captainID)
		return result, nil, err
	case "captain-current":
		result, err := h.App.CurrentCaptain(ctx)
		return result, nil, err
	case "captain-register":
		request, issues := contract.ValidateCaptainRegistration(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) > 0 {
			return nil, issues, nil
		}
		result, err := h.App.RegisterCaptain(ctx, request, key)
		return result, nil, err
	case "captain-heartbeat":
		request, issues := contract.ValidateCaptainHeartbeat(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) > 0 {
			return nil, issues, nil
		}
		result, err := h.App.CaptainHeartbeat(ctx, target.captainID, request)
		return result, nil, err
	case "captain-reinforce":
		request, issues := contract.ValidateReinforcement(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) > 0 {
			return nil, issues, nil
		}
		result, err := h.App.CaptainReinforcement(ctx, target.captainID, request)
		return result, nil, err
	case "captain-action":
		request, issues := contract.ValidateCaptainAction(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) > 0 {
			return nil, issues, nil
		}
		result, err := h.App.CaptainAction(ctx, target.itemID, request, key)
		return result, nil, err
	default:
		return nil, nil, fmt.Errorf("Unsupported Captain operation %q", target.kind)
	}
}
