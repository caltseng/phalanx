package httpapi

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProxySchemeRetainsUvicornPeerTrustAndHeaderSemantics(t *testing.T) {
	for _, test := range []struct {
		name, trust, peer string
		proto             []string
		want              string
	}{
		{"default-peer", "127.0.0.1", "127.0.0.1:42", []string{"https"}, "https"},
		{"untrusted", "127.0.0.1", "192.0.2.1:42", []string{"https"}, "http"},
		{"other-loopback", "127.0.0.1", "127.0.0.2:42", []string{"https"}, "http"},
		{"ipv6-not-default", "127.0.0.1", "[::1]:42", []string{"https"}, "http"},
		{"ipv6", "::1", "[::1]:42", []string{"https"}, "https"},
		{"network", "192.0.2.0/24, ::1", "192.0.2.3:42", []string{"https"}, "https"},
		{"invalid-network", "192.0.2.1/24", "192.0.2.3:42", []string{"https"}, "http"},
		{"mapped-not-ipv4", "127.0.0.1", "[::ffff:127.0.0.1]:42", []string{"https"}, "http"},
		{"empty-trust", "", "127.0.0.1:42", []string{"https"}, "http"},
		{"wildcard-opt-in", "*", "192.0.2.1:42", []string{"https"}, "https"},
		{"wildcard-not-list", "*,127.0.0.1", "192.0.2.1:42", []string{"https"}, "http"},
		{"case-sensitive", "127.0.0.1", "127.0.0.1:42", []string{"HTTPS"}, "http"},
		{"not-a-list", "127.0.0.1", "127.0.0.1:42", []string{"http,https"}, "http"},
		{"trimmed", "127.0.0.1", "127.0.0.1:42", []string{" https "}, "https"},
		{"latin1-space", "127.0.0.1", "127.0.0.1:42", []string{"\xa0https\xa0"}, "https"},
		{"last-value", "127.0.0.1", "127.0.0.1:42", []string{"http", "https"}, "https"},
		{"invalid-last", "127.0.0.1", "127.0.0.1:42", []string{"https", "invalid"}, "http"},
		{"websocket-value", "127.0.0.1", "127.0.0.1:42", []string{"wss"}, "wss"},
		{"direct", "127.0.0.1", "127.0.0.1:42", nil, "http"},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest("GET", "http://localhost/v1/owner/status/", nil)
			request.RemoteAddr = test.peer
			for _, value := range test.proto {
				request.Header.Add("X-Forwarded-Proto", value)
			}
			ProxyHeaders(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if got := scheme(r); got != test.want {
					t.Fatalf("scheme %q != %q", got, test.want)
				}
				if r.RemoteAddr != test.peer || r.Host != "localhost" {
					t.Fatal("proxy scheme changed client/host authority")
				}
				if len(test.proto) > 0 && (&Handler{LocalBoard: true}).localBoardAuthority(r) {
					t.Fatal("proxy metadata granted local-board authority")
				}
			}), test.trust).ServeHTTP(httptest.NewRecorder(), request)
		})
	}
	request := httptest.NewRequest("GET", "https://localhost/", nil)
	request.TLS = &tls.ConnectionState{}
	ProxyHeaders(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if scheme(r) != "https" {
			t.Fatal("direct TLS changed")
		}
	}), "").ServeHTTP(httptest.NewRecorder(), request)
}
