package application

import (
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"unicode"
)

var nativeID = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
var ipvFutureHost = regexp.MustCompile(`^v[0-9A-Fa-f]+\..+$`)

var nativePrefixes = []struct{ runtime, prefix string }{
	{"copilot-app", "ghapp://sessions/"},
	{"codex", "codex://threads/"},
}

func sessionIdentity(reference string) string {
	for _, known := range nativePrefixes {
		if strings.HasPrefix(reference, known.prefix) {
			id := strings.TrimPrefix(reference, known.prefix)
			if nativeID.MatchString(id) {
				return id
			}
		}
	}
	return reference
}

type sessionKey struct{ runtime, id string }

func recordedSessionKey(reference, runtime string) sessionKey {
	if runtime == "" {
		for _, known := range nativePrefixes {
			if strings.HasPrefix(reference, known.prefix) {
				runtime = known.runtime
				break
			}
		}
	}
	return sessionKey{runtime, sessionIdentity(reference)}
}

func sessionTarget(reference, runtime, identity, availability string, explicitURL bool) map[string]any {
	if availability == "missing" || availability == "deleted" {
		return map[string]any{"url": nil, "reason": "session " + availability}
	}
	prefix := ""
	for _, known := range nativePrefixes {
		if known.runtime == runtime {
			prefix = known.prefix
		}
	}
	var target *string
	if reference != "" {
		native := sessionIdentity(reference)
		if native != reference && (prefix == "" || strings.HasPrefix(reference, prefix)) &&
			(identity == "" || strings.EqualFold(identity, native)) {
			target = &reference
		} else if strings.HasPrefix(reference, "https://") && !strings.ContainsRune(reference, '\\') {
			space := false
			for _, r := range reference {
				space = space || unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f
			}
			if !space {
				parsed, err := url.Parse(reference)
				if err == nil && parsed.Hostname() != "" {
					valid := true
					if parsed.User != nil {
						password, _ := parsed.User.Password()
						valid = parsed.User.Username() == "" && password == ""
					}
					if port := parsed.Port(); port != "" {
						number, err := strconv.Atoi(port)
						valid = valid && err == nil && number >= 0 && number <= 65535
					}
					if strings.HasPrefix(parsed.Host, "[") {
						host := parsed.Hostname()
						valid = valid && (net.ParseIP(host) != nil || ipvFutureHost.MatchString(host))
					}
					if valid {
						target = &reference
					}
				}
			}
		} else if !explicitURL && prefix != "" && nativeID.MatchString(reference) &&
			(identity == "" || strings.EqualFold(identity, reference)) {
			value := prefix + reference
			target = &value
		}
	} else if prefix != "" && nativeID.MatchString(identity) {
		value := prefix + identity
		target = &value
	}
	reason := "session unavailable: host or identity unknown, or invalid link"
	if target != nil {
		reason = "recorded session; existence unverified"
		if availability == "present" {
			reason = "session last reported present"
		}
	}
	return map[string]any{"url": target, "reason": reason}
}
