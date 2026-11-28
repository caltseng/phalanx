package httpapi

import (
	"embed"
	"net/http"
	"strconv"
)

// The route inventory originated in P0/D0; ValidationError documents the native
// loc/msg contract. Historical oracle documents remain separate and immutable.
//
//go:embed contract-docs/*
var contractDocumentation embed.FS

func (h *Handler) serveDocumentation(w http.ResponseWriter, r *http.Request) {
	file := map[string]string{"/openapi.json": "openapi.json", "/docs": "docs.html",
		"/docs/oauth2-redirect": "oauth2.html", "/redoc": "redoc.html"}[r.URL.Path]
	content, err := contractDocumentation.ReadFile("contract-docs/" + file)
	if err != nil {
		h.failure(w, err)
		return
	}
	contentType := "text/html; charset=utf-8"
	if file == "openapi.json" {
		contentType = "application/json"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(content)))
	w.WriteHeader(200)
	w.Write(content)
}
