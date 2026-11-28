package httpapi

import (
	"errors"
	"fmt"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

const scribeCSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
	"connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"

func (h *Handler) serveAsset(w http.ResponseWriter, r *http.Request, shell bool) {
	if r.Method != "GET" && r.Method != "HEAD" {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, 405, map[string]any{"detail": "Method Not Allowed"})
		return
	}
	name := "operator-board.html"
	if !shell {
		name = strings.TrimPrefix(r.URL.Path, "/operator/assets/")
	}
	if !filepath.IsLocal(name) || strings.ContainsRune(name, 0) {
		writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		return
	}
	if h.AssetsDirectory == "" {
		if shell {
			h.failure(w, errors.New("Scribe assets are not configured"))
		} else {
			writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		}
		return
	}
	directory, err := filepath.EvalSymlinks(h.AssetsDirectory)
	if err != nil {
		h.failure(w, err)
		return
	}
	path := filepath.Join(directory, filepath.FromSlash(name))
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		if !shell && (os.IsNotExist(err) || errors.Is(err, syscall.ENAMETOOLONG) || errors.Is(err, syscall.EINVAL)) {
			writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		} else if !shell && os.IsPermission(err) {
			writeJSON(w, 401, map[string]any{"detail": "Unauthorized"})
		} else {
			h.failure(w, err)
		}
		return
	}
	if resolved != path {
		writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		return
	}
	info, err := os.Stat(path)
	if err != nil {
		h.failure(w, err)
		return
	}
	if !info.Mode().IsRegular() {
		writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		return
	}
	root, err := os.OpenRoot(directory)
	if err != nil {
		h.failure(w, err)
		return
	}
	defer root.Close()
	// Root confines the open even if a path component changes after the symlink check.
	file, err := root.Open(name)
	if err != nil {
		h.failure(w, err)
		return
	}
	defer file.Close()
	info, err = file.Stat()
	if err != nil {
		h.failure(w, err)
		return
	}
	if !info.Mode().IsRegular() {
		writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		return
	}
	contentType := mime.TypeByExtension(filepath.Ext(name))
	if contentType == "" {
		contentType = "text/plain; charset=utf-8"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("ETag", fmt.Sprintf(`"%x-%x"`, info.ModTime().UnixNano(), info.Size()))
	w.Header().Set("Content-Security-Policy", scribeCSP)
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	http.ServeContent(w, r, name, info.ModTime(), file)
}
