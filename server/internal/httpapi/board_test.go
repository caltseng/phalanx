package httpapi

import (
	"net/http/httptest"
	"strings"
	"testing"
)

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

func TestStaticRangesKeepRefusalPrecedenceAndBoundedMultipart(t *testing.T) {
	if _, status, _ := fileRanges("bytes=9-0,999-", 100); status != 416 {
		t.Fatal("unsatisfied range no longer precedes reversed-range error", status)
	}
	ranges, status, _ := fileRanges("bytes=30-39,0-9,8-29", 100)
	if status != 0 || len(ranges) != 1 || ranges[0].start != 0 || ranges[0].end != 40 {
		t.Fatal("overlapping/adjacent ranges were not sorted and merged", ranges, status)
	}
	ranges, status, _ = fileRanges("bytes="+strings.Repeat("0-1,", 100)+"0-1", 100)
	if status != 0 || len(ranges) != 0 {
		t.Fatal("over-limit range list did not fall back to complete file", ranges, status)
	}
	if _, status, message := fileRanges("items=0-1", 100); status != 400 || message != "Only support bytes range" {
		t.Fatal(status, message)
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
