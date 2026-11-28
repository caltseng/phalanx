package contract

import (
	"sort"
	"strings"
)

// LegacyFilterDigest retains existing filtered cursor scopes until their
// replacement. It is not a request decoder or a persistence encoder.
func LegacyFilterDigest(filters map[string]string) (string, error) {
	fields := make([]string, 0, len(filters))
	keys := make([]string, 0, len(filters))
	for key := range filters {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		name, err := Canonical(key)
		if err != nil {
			return "", err
		}
		value, err := Canonical(filters[key])
		if err != nil {
			return "", err
		}
		fields = append(fields, name+": "+value)
	}
	return Digest("{" + strings.Join(fields, ", ") + "}"), nil
}
