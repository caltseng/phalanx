package application

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"unicode"

	"github.com/caltseng/phalanx/server/internal/contract"
)

type reprTuple []any
type reprEntry struct {
	Key   string
	Value any
}
type reprDict []reprEntry

func pythonRepr(value any) (string, error) {
	var buffer bytes.Buffer
	err := writeRepr(&buffer, value)
	return buffer.String(), err
}

func reprString(buffer *bytes.Buffer, value string) {
	quote := '\''
	hasSingle, hasDouble := false, false
	for _, character := range value {
		hasSingle = hasSingle || character == '\''
		hasDouble = hasDouble || character == '"'
	}
	if hasSingle && !hasDouble {
		quote = '"'
	}
	buffer.WriteRune(quote)
	for _, character := range value {
		switch {
		case character == quote || character == '\\':
			buffer.WriteRune('\\')
			buffer.WriteRune(character)
		case character == '\n':
			buffer.WriteString(`\n`)
		case character == '\r':
			buffer.WriteString(`\r`)
		case character == '\t':
			buffer.WriteString(`\t`)
		case !unicode.IsPrint(character):
			if character <= 0xff {
				fmt.Fprintf(buffer, `\x%02x`, character)
			} else if character <= 0xffff {
				fmt.Fprintf(buffer, `\u%04x`, character)
			} else {
				fmt.Fprintf(buffer, `\U%08x`, character)
			}
		default:
			buffer.WriteRune(character)
		}
	}
	buffer.WriteRune(quote)
}

func writeRepr(buffer *bytes.Buffer, value any) error {
	switch value := value.(type) {
	case nil:
		buffer.WriteString("None")
	case string:
		reprString(buffer, value)
	case bool:
		if value {
			buffer.WriteString("True")
		} else {
			buffer.WriteString("False")
		}
	case reprTuple:
		buffer.WriteByte('(')
		for index, entry := range value {
			if index > 0 {
				buffer.WriteString(", ")
			}
			if err := writeRepr(buffer, entry); err != nil {
				return err
			}
		}
		if len(value) == 1 {
			buffer.WriteByte(',')
		}
		buffer.WriteByte(')')
	case []any:
		buffer.WriteByte('[')
		for index, entry := range value {
			if index > 0 {
				buffer.WriteString(", ")
			}
			if err := writeRepr(buffer, entry); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	case reprDict:
		buffer.WriteByte('{')
		for index, entry := range value {
			if index > 0 {
				buffer.WriteString(", ")
			}
			reprString(buffer, entry.Key)
			buffer.WriteString(": ")
			if err := writeRepr(buffer, entry.Value); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	case json.Number:
		text, err := contract.Canonical(value)
		if err != nil {
			return err
		}
		buffer.WriteString(text)
	case int:
		buffer.WriteString(strconv.Itoa(value))
	case int64:
		buffer.WriteString(strconv.FormatInt(value, 10))
	case map[string]any:
		entries := reprDict{}
		for _, key := range contract.ObjectKeys(value) {
			entries = append(entries, reprEntry{key, value[key]})
		}
		return writeRepr(buffer, entries)
	default:
		body, ok := contract.ObjectValues(value)
		if ok {
			entries := reprDict{}
			for _, key := range contract.ObjectKeys(value) {
				entries = append(entries, reprEntry{key, body[key]})
			}
			return writeRepr(buffer, entries)
		}
		data, err := json.Marshal(value)
		if err != nil {
			return err
		}
		decoded, _, err := contract.Decode(data)
		if err != nil {
			return err
		}
		return writeRepr(buffer, decoded)
	}
	return nil
}
