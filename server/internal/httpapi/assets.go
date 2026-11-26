package httpapi

import (
	"bytes"
	"crypto/md5"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"

	"github.com/caltseng/phalanx/server/internal/contract"
)

const scribeCSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
	"connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"

type fileRange struct{ start, end int64 }

var rangeInteger = regexp.MustCompile(`^[+-]?[0-9](?:_?[0-9])*$`)

func parseRangeInteger(value string) (*big.Int, bool) {
	value = strings.TrimSpace(value)
	if !rangeInteger.MatchString(value) {
		return nil, false
	}
	value = strings.ReplaceAll(value, "_", "")
	digits := strings.TrimLeft(strings.TrimLeft(value, "+-"), "0")
	if len(digits) > 4300 {
		return nil, false
	}
	result, ok := new(big.Int).SetString(value, 10)
	return result, ok
}

func fileRanges(header string, size int64) ([]fileRange, int, string) {
	unit, input, found := strings.Cut(header, "=")
	if !found {
		return nil, 400, "Malformed range header."
	}
	if strings.ToLower(strings.TrimSpace(unit)) != "bytes" {
		return nil, 400, "Only support bytes range"
	}
	if strings.Count(input, ",")+1 > 100 {
		return nil, 0, ""
	}
	type parsedRange struct{ start, end *big.Int }
	parsed := []parsedRange{}
	for _, part := range strings.Split(input, ",") {
		part = strings.TrimSpace(part)
		if part == "" || part == "-" {
			continue
		}
		startText, endText, found := strings.Cut(part, "-")
		if !found {
			continue
		}
		startText, endText = strings.TrimSpace(startText), strings.TrimSpace(endText)
		var start *big.Int
		end := big.NewInt(size)
		if startText == "" {
			suffix, ok := parseRangeInteger(endText)
			if !ok {
				continue
			}
			start = new(big.Int).Sub(big.NewInt(size), suffix)
			if start.Sign() < 0 {
				start.SetInt64(0)
			}
		} else {
			value, ok := parseRangeInteger(startText)
			if !ok {
				continue
			}
			start = value
			if endText != "" {
				value, ok := parseRangeInteger(endText)
				if !ok {
					continue
				}
				if value.Cmp(big.NewInt(size)) < 0 {
					end = new(big.Int).Add(value, big.NewInt(1))
				}
			}
		}
		parsed = append(parsed, parsedRange{start, end})
	}
	if len(parsed) == 0 {
		return nil, 400, "Range header: range must be requested"
	}
	for _, part := range parsed {
		if part.start.Sign() < 0 || part.start.Cmp(big.NewInt(size)) >= 0 {
			return nil, 416, ""
		}
	}
	ranges := []fileRange{}
	for _, part := range parsed {
		if part.start.Cmp(part.end) >= 0 {
			return nil, 400, "Range header: start must be less than end"
		}
		ranges = append(ranges, fileRange{part.start.Int64(), part.end.Int64()})
	}
	sort.Slice(ranges, func(i, j int) bool {
		return ranges[i].start < ranges[j].start || ranges[i].start == ranges[j].start && ranges[i].end < ranges[j].end
	})
	merged := []fileRange{ranges[0]}
	for _, next := range ranges[1:] {
		last := &merged[len(merged)-1]
		if next.start <= last.end {
			last.end = max(last.end, next.end)
		} else {
			merged = append(merged, next)
		}
	}
	return merged, 0, ""
}

func plainFileError(w http.ResponseWriter, r *http.Request, code int, text string, size int64) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(text)))
	if code == 416 {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", size))
	}
	w.WriteHeader(code)
	if r.Method != "HEAD" {
		w.Write([]byte(text))
	}
}

func fileContentType(path string, shell bool) string {
	if shell {
		return "text/html; charset=utf-8"
	}
	kind := mime.TypeByExtension(filepath.Ext(path))
	if kind == "" {
		kind = "text/plain; charset=utf-8"
	}
	if strings.HasPrefix(kind, "text/") && !strings.Contains(strings.ToLower(kind), "charset=") {
		kind += "; charset=utf-8"
	}
	return kind
}

func staticNotModified(r *http.Request, tag, modified string) bool {
	if value := r.Header.Get("If-None-Match"); value != "" {
		if strings.TrimSpace(value) == "*" {
			return true
		}
		for _, value := range strings.Split(value, ",") {
			if strings.TrimPrefix(strings.TrimSpace(value), "W/") == tag {
				return true
			}
		}
		return false
	}
	if value := r.Header.Get("If-Modified-Since"); value != "" {
		at, err := http.ParseTime(value)
		stamp, stampErr := http.ParseTime(modified)
		return err == nil && stampErr == nil && !at.Before(stamp)
	}
	return false
}

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
	if strings.ContainsRune(name, 0) || h.AssetsDirectory == "" {
		if shell {
			h.failure(w, errors.New("Scribe assets are not configured"))
		} else {
			writeJSON(w, 404, map[string]any{"detail": "Not Found"})
		}
		return
	}
	root, err := filepath.EvalSymlinks(h.AssetsDirectory)
	if err != nil {
		h.failure(w, err)
		return
	}
	path := filepath.Join(root, filepath.FromSlash(name))
	path, err = filepath.EvalSymlinks(path)
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
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
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
	size := info.Size()
	mtime := float64(info.ModTime().Unix()) + float64(info.ModTime().Nanosecond())/1000000000
	value, err := contract.Canonical(json.Number(strconv.FormatFloat(mtime, 'g', -1, 64)))
	if err != nil {
		h.failure(w, err)
		return
	}
	digest := md5.Sum([]byte(value + "-" + strconv.FormatInt(size, 10)))
	tag := `"` + hex.EncodeToString(digest[:]) + `"`
	modified := info.ModTime().UTC().Format(http.TimeFormat)
	if !shell && staticNotModified(r, tag, modified) {
		w.Header().Set("ETag", tag)
		w.WriteHeader(304)
		return
	}
	var ranges []fileRange
	if header, exists := r.Header["Range"]; exists && len(header) > 0 {
		ifRange, present := r.Header["If-Range"]
		useRange := !present || len(ifRange) > 0 && !strings.HasPrefix(ifRange[0], "W/") &&
			(ifRange[0] == modified || ifRange[0] == tag)
		if useRange {
			var code int
			var detail string
			ranges, code, detail = fileRanges(headerText(header[0]), size)
			if code != 0 {
				plainFileError(w, r, code, detail, size)
				return
			}
		}
	}
	contentType := fileContentType(path, shell)
	file, err := os.Open(path)
	if err != nil {
		h.failure(w, err)
		return
	}
	defer file.Close()
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("ETag", tag)
	w.Header().Set("Last-Modified", modified)
	w.Header().Set("Accept-Ranges", "bytes")
	if shell {
		w.Header().Set("Content-Security-Policy", scribeCSP)
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
	}
	if len(ranges) == 0 {
		w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
		w.WriteHeader(200)
		if r.Method != "HEAD" {
			if _, err := io.Copy(w, file); err != nil && h.Logger != nil {
				h.Logger.Error("Could not stream Scribe asset", "error", err)
			}
		}
		return
	}
	if len(ranges) == 1 {
		part := ranges[0]
		w.Header().Set("Content-Length", strconv.FormatInt(part.end-part.start, 10))
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", part.start, part.end-1, size))
		w.WriteHeader(206)
		if r.Method != "HEAD" {
			if _, err := io.Copy(w, io.NewSectionReader(file, part.start, part.end-part.start)); err != nil && h.Logger != nil {
				h.Logger.Error("Could not stream Scribe range", "error", err)
			}
		}
		return
	}
	random := make([]byte, 13)
	if _, err := rand.Read(random); err != nil {
		h.failure(w, err)
		return
	}
	boundary := hex.EncodeToString(random)
	var body bytes.Buffer
	for _, part := range ranges {
		fmt.Fprintf(&body, "--%s\r\nContent-Type: %s\r\nContent-Range: bytes %d-%d/%d\r\n\r\n",
			boundary, contentType, part.start, part.end-1, size)
		if _, err := io.Copy(&body, io.NewSectionReader(file, part.start, part.end-part.start)); err != nil {
			h.failure(w, err)
			return
		}
		body.WriteString("\r\n")
	}
	fmt.Fprintf(&body, "--%s--", boundary)
	w.Header().Set("Content-Type", "multipart/byteranges; boundary="+boundary)
	w.Header().Set("Content-Length", strconv.Itoa(body.Len()))
	w.WriteHeader(206)
	if r.Method != "HEAD" {
		w.Write(body.Bytes())
	}
}
