package httpapi

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestOpenAPIDocumentsNativeValidationIssues(t *testing.T) {
	response := httptest.NewRecorder()
	(&Handler{}).ServeHTTP(response, httptest.NewRequest("GET", "/openapi.json", nil))
	var document struct {
		Components struct {
			Schemas map[string]struct {
				Properties map[string]json.RawMessage `json:"properties"`
				Required   []string                   `json:"required"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if response.Code != 200 || json.Unmarshal(response.Body.Bytes(), &document) != nil {
		t.Fatal(response.Code, response.Body.String())
	}
	schema := document.Components.Schemas["ValidationError"]
	if len(schema.Properties) != 2 || schema.Properties["loc"] == nil || schema.Properties["msg"] == nil ||
		!reflect.DeepEqual(schema.Required, []string{"loc", "msg"}) {
		t.Fatal(schema)
	}
}

func TestValidationDetailsContainOnlyFieldLocationAndMessage(t *testing.T) {
	handler := &Handler{App: fixtureApp(t), Owner: "owner", Runner: "runner"}
	for _, test := range []struct {
		method, path, body string
		loc                []any
	}{
		{"POST", "/v1/items", `{"repository_id":"fixture"}`, []any{"body", "goal"}},
		{"POST", "/v1/items", `{"repository_id":"fixture","goal":""}`, []any{"body", "goal"}},
		{"POST", "/v1/items", `{"repository_id":"fixture","goal":"ok","label":"bad label"}`, []any{"body", "label"}},
		{"POST", "/v1/items", `{"repository_id":"fixture","goal":"ok","dependencies":[{"upstream_id":false}]}`, []any{"body", "dependencies", float64(0), "upstream_id"}},
		{"POST", "/v1/items", `{"goal":}`, []any{"body"}},
		{"POST", "/v1/items", `{} {}`, []any{"body"}},
		{"GET", "/v1/owner/inbox?limit=1_0", "", []any{"query", "limit"}},
		{"GET", "/v1/operator/executions?closed_after=1700000000", "", []any{"query", "closed_after"}},
		{"GET", "/v1/operator/executions?closed_after=2026-11-27T12:34:56%2B24:00", "", []any{"query", "closed_after"}},
		{"GET", "/v1/operator/executions?closed_after=2026-11-27T12:34:56%2B00:60", "", []any{"query", "closed_after"}},
		{"GET", "/v1/operator/executions?closed_after=2026-11-27T1:34:56Z", "", []any{"query", "closed_after"}},
		{"GET", "/v1/operator/executions?closed_after=2026-11-27T12:34:56,123Z", "", []any{"query", "closed_after"}},
	} {
		t.Run(test.path+"/"+test.body, func(t *testing.T) {
			request := httptest.NewRequest(test.method, test.path, strings.NewReader(test.body))
			request.Header.Set("Authorization", "Bearer owner")
			request.Header.Set("Content-Type", "application/json")
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			var body map[string][]map[string]any
			if response.Code != 422 || json.Unmarshal(response.Body.Bytes(), &body) != nil || len(body) != 1 || len(body["detail"]) != 1 {
				t.Fatal(response.Code, response.Body.String())
			}
			issue := body["detail"][0]
			if len(issue) != 2 || !reflect.DeepEqual(issue["loc"], test.loc) || issue["msg"] == "" {
				t.Fatal(issue)
			}
		})
	}
}

func TestHTTPReplaysHistoricalReceiptWithNativeDecodedRequest(t *testing.T) {
	app := fixtureApp(t)
	const digest = "791935e974e970ab8cab68ccceda94588076f93da0e0aa34391e76c9f45e9ac8"
	const snapshot = `{"id":"legacy","note":"\u00e9 <>&","number":1.0}`
	if err := app.Store.Write(context.Background(), func(tx *sqlite.Tx) error {
		return tx.SaveReceipt("intake", "old-receipt", digest, snapshot)
	}); err != nil {
		t.Fatal(err)
	}
	handler := &Handler{App: app, Owner: "owner", Runner: "runner"}
	for range 2 {
		request := httptest.NewRequest("POST", "/v1/items", strings.NewReader(`{"goal":"Legacy","repository_id":"fixture"}`))
		request.Header.Set("Authorization", "Bearer owner")
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Idempotency-Key", "old-receipt")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != 201 || !strings.Contains(response.Body.String(), `"id":"legacy"`) ||
			!strings.Contains(response.Body.String(), `"number":1.0`) {
			t.Fatal(response.Code, response.Body.String())
		}
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		receipt, err := tx.Receipt("intake", "old-receipt")
		if err == nil && (receipt.Digest != digest || receipt.Response != snapshot) {
			t.Fatal("replay rewrote historical receipt", receipt)
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}
