package httpapi

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func (h *Handler) writerOperation(ctx context.Context, target route, writerID string, input any, keys []string,
	pathIssues []contract.Issue, key *string) (json.RawMessage, []contract.Issue, error) {
	switch target.kind {
	case "writer-register":
		request, issues := contract.ValidateWriterRegistration(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.RegisterWriter(ctx, target.itemID, target.executionID, request)
		return result, nil, err
	case "writer-update":
		request, issues := contract.ValidateWriterUpdate(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.UpdateWriter(ctx, target.itemID, target.executionID, request)
		return result, nil, err
	case "writer-withdraw":
		request, issues := contract.ValidateWriterWithdrawal(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.WithdrawWriter(ctx, target.itemID, target.executionID, request)
		return result, nil, err
	case "writer-progress":
		request, issues := contract.ValidateWriterProgress(input, keys)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.WriterProgress(ctx, writerID, request)
		return result, nil, err
	case "writer-capacity":
		request, issues := contract.ValidateWriterCapacity(input, keys)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.WriterCapacity(ctx, writerID, request)
		return result, nil, err
	case "writer-attempt":
		result, err := h.App.WriterNext(ctx, writerID)
		return result, nil, err
	case "writer-result":
		request, issues := contract.ValidateWriterResult(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.WriterResult(ctx, target.itemID, target.executionID, writerID, request, key)
		return result, nil, err
	case "writer-stop":
		request, issues := contract.ValidateAttemptEvent(input, keys)
		issues = append(pathIssues, issues...)
		if len(issues) != 0 {
			return nil, issues, nil
		}
		result, err := h.App.WriterStopConfirmed(ctx, target.itemID, target.executionID, writerID, request, key)
		return result, nil, err
	default:
		return nil, nil, fmt.Errorf("Unsupported writer operation %q", target.kind)
	}
}
