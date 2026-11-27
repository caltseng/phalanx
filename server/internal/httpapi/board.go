package httpapi

import (
	"net"
	"net/http"
	"regexp"
	"strings"
)

var directLocalHost = regexp.MustCompile(`(?i)^(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?$`)

func boardRead(kind string) bool {
	switch kind {
	case "operator-list", "operator-overview", "operator-item", "operator-group", "operator-timeline",
		"dispatch", "captains", "captain", "captain-current", "flags", "scout-status", "report":
		return true
	default:
		return false
	}
}

func boardAction(kind string) bool {
	return kind == "board-repositories" || kind == "board-intake" || kind == "board-dispatch" ||
		kind == "board-set-dispatch" || kind == "board-scout-decision"
}

func (h *Handler) localBoardAuthority(r *http.Request) bool {
	if !h.LocalBoard {
		return false
	}
	if _, present := r.Header["Authorization"]; present {
		return false
	}
	if hosts := r.Header.Values("Host"); len(hosts) > 1 {
		return false
	}
	for name := range r.Header {
		name = strings.ToLower(name)
		if name == "forwarded" || strings.HasPrefix(name, "x-forwarded-") || name == "x-real-ip" {
			return false
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback() && directLocalHost.MatchString(headerText(r.Host))
}

func localBoardOrigin(r *http.Request) bool {
	origins, sites := r.Header.Values("Origin"), r.Header.Values("Sec-Fetch-Site")
	if len(origins) > 1 || len(sites) > 1 {
		return false
	}
	expected := scheme(r) + "://" + headerText(r.Host)
	origin := ""
	if len(origins) == 1 {
		origin = headerText(origins[0])
		if origin != expected {
			return false
		}
	}
	if len(sites) == 1 && headerText(sites[0]) != "same-origin" {
		return false
	}
	return r.Method == "GET" || r.Method == "HEAD" || origin == expected
}
