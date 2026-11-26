package contract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"sort"
	"strconv"
	"unicode/utf16"
)

// Decode keeps numeric input exact and top-level key order for extra-field
// errors. The last duplicate key wins, as in Python json.loads.
func Decode(data []byte) (any, []string, error) {
	return decode(data)
}

func ObjectKeys(value any) []string {
	if object, ok := value.(*orderedObject); ok {
		return object.keys
	}
	keys := []string{}
	if object, ok := value.(map[string]any); ok {
		for key := range object {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	return keys
}

// Canonical is Python json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=True, allow_nan=False) for the slice's string/null/list objects.
// Numeric state/response values are decoded as json.Number, never float64.
func Canonical(value any) (string, error) {
	var buffer bytes.Buffer
	err := writeValue(&buffer, value)
	return buffer.String(), err
}

func writeString(buffer *bytes.Buffer, text string) {
	buffer.WriteByte('"')
	for _, r := range text {
		switch r {
		case '"', '\\':
			buffer.WriteByte('\\')
			buffer.WriteRune(r)
		case '\b':
			buffer.WriteString(`\b`)
		case '\f':
			buffer.WriteString(`\f`)
		case '\n':
			buffer.WriteString(`\n`)
		case '\r':
			buffer.WriteString(`\r`)
		case '\t':
			buffer.WriteString(`\t`)
		default:
			if r < 32 || r > 126 {
				if r <= 0xffff {
					fmt.Fprintf(buffer, `\u%04x`, r)
				} else {
					high, low := utf16.EncodeRune(r)
					fmt.Fprintf(buffer, `\u%04x\u%04x`, high, low)
				}
			} else {
				buffer.WriteRune(r)
			}
		}
	}
	buffer.WriteByte('"')
}

func writeValue(buffer *bytes.Buffer, value any) error {
	switch x := value.(type) {
	case nil:
		buffer.WriteString("null")
	case string:
		writeString(buffer, x)
	case bool:
		buffer.WriteString(strconv.FormatBool(x))
	case json.Number:
		integer, ok := new(big.Int).SetString(string(x), 10)
		if !ok {
			return fmt.Errorf("non-integer canonical number %q is outside the foundation contract", x)
		}
		buffer.WriteString(integer.String())
	case map[string]any:
		buffer.WriteByte('{')
		for i, key := range ObjectKeys(x) {
			if i > 0 {
				buffer.WriteByte(',')
			}
			writeString(buffer, key)
			buffer.WriteByte(':')
			if err := writeValue(buffer, x[key]); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	case *orderedObject:
		return writeValue(buffer, x.values)
	case []any:
		buffer.WriteByte('[')
		for i, entry := range x {
			if i > 0 {
				buffer.WriteByte(',')
			}
			if err := writeValue(buffer, entry); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	default:
		data, err := json.Marshal(value)
		if err != nil {
			return err
		}
		decoded, _, err := Decode(data)
		if err != nil {
			return err
		}
		return writeValue(buffer, decoded)
	}
	return nil
}

func Digest(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}
