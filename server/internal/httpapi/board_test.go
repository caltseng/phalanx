package httpapi

import (
	"bytes"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

var scribeAssetReference = regexp.MustCompile(`(?:src|href)="(/operator/assets/[^"]+)"`)

func TestScribeLoadsPackagedShellAndReferencedAssets(t *testing.T) {
	directory, err := filepath.Abs(filepath.Join("..", "..", "..", "phalanx", "static"))
	if err != nil {
		t.Fatal(err)
	}
	handler := Handler{AssetsDirectory: directory}
	for _, path := range []string{"/operator/", "/operator/queue", "/operator/history", "/operator/items/" + strings.Repeat("a", 32)} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest("GET", path, nil))
		shell, err := os.ReadFile(filepath.Join(directory, "operator-board.html"))
		if err != nil {
			t.Fatal(err)
		}
		if response.Code != 200 || !bytes.Equal(response.Body.Bytes(), shell) {
			t.Fatalf("%s: shell failed to load: %d", path, response.Code)
		}
		assertScribeHeaders(t, response)
		assets := scribeAssetReference.FindAllSubmatch(shell, -1)
		if len(assets) == 0 {
			t.Fatal("shell does not reference any assets")
		}
		for _, asset := range assets {
			route := string(asset[1])
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest("GET", route, nil))
			expected, err := os.ReadFile(filepath.Join(directory, strings.TrimPrefix(route, "/operator/assets/")))
			if err != nil {
				t.Fatal(err)
			}
			if response.Code != 200 || !bytes.Equal(response.Body.Bytes(), expected) {
				t.Fatalf("%s: asset failed to load: %d", route, response.Code)
			}
			assertScribeHeaders(t, response)
		}
	}
}

func assertScribeHeaders(t *testing.T, response *httptest.ResponseRecorder) {
	t.Helper()
	for name, expected := range map[string]string{
		"Content-Security-Policy": scribeCSP,
		"Referrer-Policy":         "no-referrer",
		"X-Content-Type-Options":  "nosniff",
	} {
		if actual := response.Header().Get(name); actual != expected {
			t.Fatalf("%s: got %q, want %q", name, actual, expected)
		}
	}
}

func TestStaticContentRangesAndCacheValidation(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "test.txt")
	content := "0123456789abcdefghijklmnopqrstuvwxyz"
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	stamp := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	if err := os.Chtimes(path, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	handler := Handler{AssetsDirectory: directory}
	request := func(method string, headers map[string]string) *httptest.ResponseRecorder {
		t.Helper()
		r := httptest.NewRequest(method, "/operator/assets/test.txt", nil)
		for name, value := range headers {
			r.Header.Set(name, value)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	full := request("GET", nil)
	if full.Code != 200 || full.Body.String() != content || full.Header().Get("Content-Type") != "text/plain; charset=utf-8" ||
		full.Header().Get("Content-Length") != strconv.Itoa(len(content)) || full.Header().Get("Accept-Ranges") != "bytes" {
		t.Fatalf("complete file: %d %v %q", full.Code, full.Header(), full.Body.String())
	}
	tag, modified := full.Header().Get("ETag"), full.Header().Get("Last-Modified")
	if tag == "" || modified != stamp.Format(http.TimeFormat) {
		t.Fatalf("missing cache validators: %v", full.Header())
	}
	for _, fixture := range []struct {
		name, method string
		headers      map[string]string
		status       int
		body, span   string
	}{
		{"head", "HEAD", nil, 200, "", ""},
		{"range", "GET", map[string]string{"Range": "bytes=2-5"}, 206, "2345", "bytes 2-5/36"},
		{"head-range", "HEAD", map[string]string{"Range": "bytes=2-5"}, 206, "", "bytes 2-5/36"},
		{"suffix", "GET", map[string]string{"Range": "bytes=-3"}, 206, "xyz", "bytes 33-35/36"},
		{"unsatisfied", "GET", map[string]string{"Range": "bytes=99-"}, 416, "", "bytes */36"},
		{"reversed", "GET", map[string]string{"Range": "bytes=9-0"}, 416, "", ""},
		{"wrong-unit", "GET", map[string]string{"Range": "items=0-1"}, 416, "", ""},
		{"bounded-amplification", "GET", map[string]string{"Range": "bytes=" + strings.Repeat("0-35,", 100) + "0-35"}, 200, content, ""},
		{"tag", "GET", map[string]string{"If-None-Match": tag}, 304, "", ""},
		{"weak-tag", "GET", map[string]string{"If-None-Match": "W/" + tag}, 304, "", ""},
		{"modified", "GET", map[string]string{"If-Modified-Since": modified}, 304, "", ""},
		{"tag-precedence", "GET", map[string]string{"If-None-Match": `"other"`, "If-Modified-Since": modified}, 200, content, ""},
		{"failed-precondition", "GET", map[string]string{"If-Match": `"other"`}, 412, "", ""},
		{"if-range", "GET", map[string]string{"Range": "bytes=2-5", "If-Range": tag}, 206, "2345", "bytes 2-5/36"},
		{"stale-if-range", "GET", map[string]string{"Range": "bytes=2-5", "If-Range": `"other"`}, 200, content, ""},
		{"weak-if-range", "GET", map[string]string{"Range": "bytes=2-5", "If-Range": "W/" + tag}, 200, content, ""},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			response := request(fixture.method, fixture.headers)
			if response.Code != fixture.status || response.Header().Get("Content-Range") != fixture.span {
				t.Fatalf("%d %v %q", response.Code, response.Header(), response.Body.String())
			}
			if fixture.status < 400 && response.Body.String() != fixture.body {
				t.Fatalf("body: got %q, want %q", response.Body.String(), fixture.body)
			}
			if fixture.name == "head" && response.Header().Get("Content-Length") != strconv.Itoa(len(content)) {
				t.Fatal("HEAD lost the full content length")
			}
			assertScribeHeaders(t, response)
		})
	}
	response := request("GET", map[string]string{"Range": "bytes=20-22,0-2"})
	kind, parameters, err := mime.ParseMediaType(response.Header().Get("Content-Type"))
	if err != nil || response.Code != 206 || kind != "multipart/byteranges" {
		t.Fatalf("multipart: %d %v %v", response.Code, response.Header(), err)
	}
	reader := multipart.NewReader(response.Body, parameters["boundary"])
	for _, expected := range []struct{ span, body string }{{"bytes 20-22/36", "klm"}, {"bytes 0-2/36", "012"}} {
		part, err := reader.NextPart()
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(part)
		if err != nil || string(body) != expected.body || part.Header.Get("Content-Range") != expected.span {
			t.Fatalf("part: %v %q %v", part.Header, body, err)
		}
	}
	if _, err := reader.NextPart(); err != io.EOF {
		t.Fatalf("unexpected extra multipart data: %v", err)
	}
}

func TestStaticRefusesTraversalDirectoriesAndSymlinks(t *testing.T) {
	directory := t.TempDir()
	assets := filepath.Join(directory, "assets")
	if err := os.Mkdir(assets, 0700); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{filepath.Join(directory, "secret.txt"), filepath.Join(assets, "safe.txt")} {
		if err := os.WriteFile(path, []byte("not disclosed"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	handler := Handler{AssetsDirectory: assets}
	refused := func(name string) {
		t.Helper()
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest("GET", "/operator/assets/"+name, nil))
		if response.Code != 404 || strings.Contains(response.Body.String(), "not disclosed") {
			t.Fatalf("%s: %d %q", name, response.Code, response.Body.String())
		}
	}
	for _, name := range []string{"../secret.txt", "%2e%2e/secret.txt", "..%5csecret.txt", "/secret.txt", "%00", ".", "missing.txt"} {
		refused(name)
	}
	for _, fixture := range []struct{ name, target string }{
		{"outside.txt", filepath.Join(directory, "secret.txt")},
		{"inside.txt", filepath.Join(assets, "safe.txt")},
		{"nested", directory},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			if err := os.Symlink(fixture.target, filepath.Join(assets, fixture.name)); err != nil {
				t.Skipf("symlink creation unavailable: %v", err)
			}
			name := fixture.name
			if name == "nested" {
				name += "/secret.txt"
			}
			refused(name)
		})
	}
}

func TestLocalBoardAuthorityRequiresDirectPeerAndNoProxyOrAuthorization(t *testing.T) {
	handler := Handler{LocalBoard: true}
	for _, fixture := range []struct {
		name, peer, host string
		header, value    string
		allowed          bool
	}{
		{"direct", "127.0.0.1:1234", "localhost:8000", "", "", true},
		{"ipv6", "[::1]:1234", "[::1]", "", "", true},
		{"remote", "192.0.2.1:1234", "localhost:8000", "", "", false},
		{"rebound", "127.0.0.1:1234", "invalid.example", "", "", false},
		{"forwarded", "127.0.0.1:1234", "localhost", "Forwarded", "for=127.0.0.1", false},
		{"forwarded-prefix", "127.0.0.1:1234", "localhost", "X-Forwarded-Anything", "local", false},
		{"real-ip", "127.0.0.1:1234", "localhost", "X-Real-IP", "127.0.0.1", false},
		{"empty-auth", "127.0.0.1:1234", "localhost", "Authorization", "", false},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			request := httptest.NewRequest("GET", "http://localhost/v1/operator/overview", nil)
			request.RemoteAddr, request.Host = fixture.peer, fixture.host
			if fixture.header != "" {
				request.Header.Set(fixture.header, fixture.value)
			}
			if actual := handler.localBoardAuthority(request); actual != fixture.allowed {
				t.Fatal("direct local authority changed", actual, fixture.allowed)
			}
		})
	}
	request := httptest.NewRequest("GET", "http://localhost/v1/operator/overview", nil)
	request.RemoteAddr = "127.0.0.1:1234"
	request.Header.Add("Host", "localhost")
	request.Header.Add("Host", "localhost")
	if handler.localBoardAuthority(request) {
		t.Fatal("duplicate Host metadata granted direct authority")
	}
}

func TestLocalBoardMutationRequiresOneMatchingOrigin(t *testing.T) {
	read := httptest.NewRequest("GET", "http://localhost/operator/api/repositories", nil)
	if !localBoardOrigin(read) {
		t.Fatal("read unexpectedly required Origin")
	}

	write := httptest.NewRequest("POST", "http://localhost/operator/api/items", nil)
	if localBoardOrigin(write) {
		t.Fatal("anonymous local mutation accepted missing Origin")
	}
	write.Header.Set("Origin", "http://localhost")
	if !localBoardOrigin(write) {
		t.Fatal("matching direct mutation refused")
	}
	write.Header.Add("Origin", "http://localhost")
	if localBoardOrigin(write) {
		t.Fatal("duplicate Origin accepted")
	}
}

func TestRetainedDashboardPeerAndOriginVectors(t *testing.T) {
	handler := Handler{LocalBoard: true}
	for _, fixture := range []struct {
		peer, host, origin, site string
		status                   int
	}{
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8000", "same-origin", 200},
		{"[::1]:1234", "[::1]:8000", "http://[::1]:8000", "same-origin", 200},
		{"203.0.113.8:1234", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"[::ffff:203.0.113.8]:1234", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"127.0.0.1:1234", "evil.example", "http://evil.example", "same-origin", 401},
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8001", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "null", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8000", "cross-site", 403},
	} {
		request := httptest.NewRequest("POST", "http://localhost/operator/api/items", nil)
		request.RemoteAddr, request.Host = fixture.peer, fixture.host
		if fixture.origin != "" {
			request.Header.Set("Origin", fixture.origin)
		}
		request.Header.Set("Sec-Fetch-Site", fixture.site)
		status := 200
		if !handler.localBoardAuthority(request) {
			status = 401
		} else if !localBoardOrigin(request) {
			status = 403
		}
		if status != fixture.status {
			t.Fatalf("retained actual-peer/origin vector changed: %+v: %d", fixture, status)
		}
	}
}
