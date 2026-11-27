package httpapi

import (
	"context"
	"net"
	"net/http"
	"net/netip"
	"strings"
)

type proxySchemeKey struct{}

// ProxyHeaders matches Uvicorn's explicitly configured peer trust, not forwarded
// client/host claims. Forwarding headers remain visible to the local-board refusal.
func ProxyHeaders(next http.Handler, trustedHosts string) http.Handler {
	hosts := strings.Split(trustedHosts, ",")
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			peer = r.RemoteAddr
		}
		trusted := trustedHosts == "*"
		address, addressErr := netip.ParseAddr(peer)
		for _, raw := range hosts {
			host := strings.TrimSpace(raw)
			if network, err := netip.ParsePrefix(host); err == nil && network == network.Masked() {
				trusted = trusted || (addressErr == nil && network.Contains(address))
			} else if ip, err := netip.ParseAddr(host); err == nil {
				trusted = trusted || (addressErr == nil && ip == address)
			} else {
				trusted = trusted || (peer != "" && host == peer)
			}
		}
		values := r.Header.Values("X-Forwarded-Proto")
		if trusted && len(values) > 0 {
			// Uvicorn uses the last occurrence, strips whitespace, and accepts
			// only these exact lower-case values (including ws/wss on HTTP).
			value := strings.TrimSpace(headerText(values[len(values)-1]))
			switch value {
			case "http", "https", "ws", "wss":
				r = r.WithContext(context.WithValue(r.Context(), proxySchemeKey{}, value))
			}
		}
		next.ServeHTTP(w, r)
	})
}
