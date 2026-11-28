package contract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
)

func ObjectKeys(value any) []string {
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
// ensure_ascii=True, allow_nan=False). Integer tokens retain arbitrary precision;
// decimal/exponent tokens use Python json.loads' binary64 representation.
func Canonical(value any) (string, error) {
	var buffer bytes.Buffer
	err := writeValue(&buffer, value)
	return buffer.String(), err
}

// SumNumbers matches Python's integer sum and its promotion on the first real.
func SumNumbers(numbers []json.Number) (any, error) {
	total := new(big.Int)
	var real *float64
	for _, number := range numbers {
		if real == nil && !strings.ContainsAny(string(number), ".eE") {
			integer, ok := new(big.Int).SetString(string(number), 10)
			if !ok {
				return nil, fmt.Errorf("Invalid numeric subtotal %q", number)
			}
			total.Add(total, integer)
			continue
		}
		if real == nil {
			value, _ := new(big.Float).SetInt(total).Float64()
			real = &value
		}
		value, err := number.Float64()
		if err != nil {
			return nil, err
		}
		*real += value
	}
	if real != nil {
		return *real, nil
	}
	if len(numbers) == 0 {
		return nil, nil
	}
	return json.Number(total.String()), nil
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
		if x == "Infinity" || x == "-Infinity" || x == "NaN" {
			name := map[json.Number]string{"Infinity": "inf", "-Infinity": "-inf", "NaN": "nan"}[x]
			return fmt.Errorf("Out of range float values are not JSON compliant: %s", name)
		}
		if strings.ContainsAny(string(x), ".eE") {
			number, err := strconv.ParseFloat(string(x), 64)
			if err != nil || math.IsInf(number, 0) || math.IsNaN(number) {
				name := "inf"
				if number < 0 {
					name = "-inf"
				}
				return fmt.Errorf("Out of range float values are not JSON compliant: %s", name)
			}
			buffer.WriteString(pythonFloat(number))
			break
		}
		integer, ok := new(big.Int).SetString(string(x), 10)
		if !ok {
			return fmt.Errorf("invalid canonical number %q", x)
		}
		buffer.WriteString(integer.String())
	case map[string]any:
		if x == nil {
			buffer.WriteString("null")
			break
		}
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
	case []any:
		if x == nil {
			buffer.WriteString("null")
			break
		}
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

func pythonFloat(value float64) string {
	exponential := strconv.FormatFloat(value, 'e', -1, 64)
	at := strings.IndexByte(exponential, 'e')
	exponent, _ := strconv.Atoi(exponential[at+1:])
	if exponent < -4 || exponent >= 16 {
		return exponential
	}
	result := strconv.FormatFloat(value, 'f', -1, 64)
	if !strings.ContainsRune(result, '.') {
		result += ".0"
	}
	return result
}

func Digest(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}
