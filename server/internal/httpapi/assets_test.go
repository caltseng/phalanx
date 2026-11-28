package httpapi

import (
	"bytes"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestScribeAssetsServeRepositoryBytesAndSafeBoundaries(t *testing.T) {
	root := filepath.Join("..", "..", "..", "phalanx", "static")
	h := &Handler{AssetsDirectory: root}
	for _, name := range []string{"operator-board.html", "operator-board.js", "operator-board.css",
		"operator-shell.js", "operator-helpers.js", "operator-http.js", "operator-markdown.js", "phalanx-brand.svg"} {
		t.Run(name, func(t *testing.T) {
			expected, err := os.ReadFile(filepath.Join(root, name))
			if err != nil {
				t.Fatal(err)
			}
			path := "/operator/assets/" + name
			if name == "operator-board.html" {
				path = "/operator/"
			}
			r := httptest.NewRequest("GET", path, nil)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != 200 || !bytes.Equal(w.Body.Bytes(), expected) ||
				w.Header().Get("Content-Type") == "" || w.Header().Get("ETag") == "" {
				t.Fatal("Scribe bytes or metadata changed", w.Code, w.Header())
			}
			if name == "operator-board.html" {
				if w.Header().Get("Content-Security-Policy") != scribeCSP ||
					w.Header().Get("Referrer-Policy") != "no-referrer" ||
					w.Header().Get("X-Content-Type-Options") != "nosniff" {
					t.Fatal("Scribe shell lost security headers", w.Header())
				}
			} else {
				r.Header.Set("If-None-Match", w.Header().Get("ETag"))
				unchanged := httptest.NewRecorder()
				h.ServeHTTP(unchanged, r)
				if unchanged.Code != 304 || unchanged.Body.Len() != 0 {
					t.Fatal("asset conditional read", unchanged.Code)
				}
			}
			if name != "operator-board.html" {
				head := httptest.NewRecorder()
				h.ServeHTTP(head, httptest.NewRequest("HEAD", path, nil))
				if head.Code != 200 || head.Body.Len() != 0 ||
					head.Header().Get("Content-Length") != w.Header().Get("Content-Length") {
					t.Fatal("HEAD metadata/body", head.Code, head.Header())
				}
			}
			r = httptest.NewRequest("GET", path, nil)
			r.Header.Set("Range", "bytes=0-9")
			part := httptest.NewRecorder()
			h.ServeHTTP(part, r)
			if part.Code != 206 || !bytes.Equal(part.Body.Bytes(), expected[:10]) ||
				!strings.HasPrefix(part.Header().Get("Content-Range"), "bytes 0-9/") {
				t.Fatal("asset byte range", part.Code, part.Header())
			}
		})
	}
	for _, path := range []string{"/operator/assets/missing.js", "/operator/assets/../server/go.mod",
		"/operator/assets/../../pyproject.toml", "/operator/assets/"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 404 {
			t.Fatal("asset boundary", path, w.Code, w.Body.String())
		}
	}
}
