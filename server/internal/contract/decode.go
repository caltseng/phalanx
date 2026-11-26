package contract

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

type orderedObject struct {
	values map[string]any
	keys   []string
}

func (o *orderedObject) MarshalJSON() ([]byte, error) { return json.Marshal(o.values) }

type JSONError struct {
	Offset  int
	Message string
}

func (e *JSONError) Error() string { return fmt.Sprintf("%s at character %d", e.Message, e.Offset) }

type BodyError struct{}

func (*BodyError) Error() string { return "There was an error parsing the body" }

type parser struct {
	data []byte
	at   int
}

var number = regexp.MustCompile(`^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?`)

func (p *parser) whitespace() {
	for p.at < len(p.data) && strings.ContainsRune(" \n\r\t", rune(p.data[p.at])) {
		p.at++
	}
}

func (p *parser) failure(message string, offset int) error {
	return &JSONError{utf8.RuneCount(p.data[:offset]), message}
}

func decode(data []byte) (any, []string, error) {
	data = bytes.TrimPrefix(data, []byte{0xef, 0xbb, 0xbf})
	if !utf8.Valid(data) {
		return nil, nil, &BodyError{}
	}
	p := parser{data: data}
	value, err := p.value(0)
	if err != nil {
		return nil, nil, err
	}
	p.whitespace()
	if p.at != len(data) {
		return nil, nil, p.failure("Extra data", p.at)
	}
	if object, ok := value.(*orderedObject); ok {
		return object.values, object.keys, nil
	}
	return value, nil, nil
}

func (p *parser) value(depth int) (any, error) {
	if depth >= 1000 {
		return nil, &BodyError{}
	}
	p.whitespace()
	if p.at == len(p.data) {
		return nil, p.failure("Expecting value", p.at)
	}
	switch p.data[p.at] {
	case '{':
		p.at++
		p.whitespace()
		result := &orderedObject{values: map[string]any{}, keys: []string{}}
		if p.at < len(p.data) && p.data[p.at] == '}' {
			p.at++
			return result, nil
		}
		for {
			if p.at == len(p.data) || p.data[p.at] != '"' {
				return nil, p.failure("Expecting property name enclosed in double quotes", p.at)
			}
			key, err := p.text()
			if err != nil {
				return nil, err
			}
			p.whitespace()
			if p.at == len(p.data) || p.data[p.at] != ':' {
				return nil, p.failure("Expecting ':' delimiter", p.at)
			}
			p.at++
			value, err := p.value(depth + 1)
			if err != nil {
				return nil, err
			}
			if _, exists := result.values[key]; !exists {
				result.keys = append(result.keys, key)
			}
			result.values[key] = value
			p.whitespace()
			if p.at < len(p.data) && p.data[p.at] == '}' {
				p.at++
				return result, nil
			}
			if p.at == len(p.data) || p.data[p.at] != ',' {
				return nil, p.failure("Expecting ',' delimiter", p.at)
			}
			comma := p.at
			p.at++
			p.whitespace()
			if p.at < len(p.data) && p.data[p.at] == '}' {
				return nil, p.failure("Illegal trailing comma before end of object", comma)
			}
		}
	case '[':
		p.at++
		p.whitespace()
		result := []any{}
		if p.at < len(p.data) && p.data[p.at] == ']' {
			p.at++
			return result, nil
		}
		for {
			value, err := p.value(depth + 1)
			if err != nil {
				return nil, err
			}
			result = append(result, value)
			p.whitespace()
			if p.at < len(p.data) && p.data[p.at] == ']' {
				p.at++
				return result, nil
			}
			if p.at == len(p.data) || p.data[p.at] != ',' {
				return nil, p.failure("Expecting ',' delimiter", p.at)
			}
			comma := p.at
			p.at++
			p.whitespace()
			if p.at < len(p.data) && p.data[p.at] == ']' {
				return nil, p.failure("Illegal trailing comma before end of array", comma)
			}
		}
	case '"':
		return p.text()
	}
	for _, literal := range []struct {
		text  string
		value any
	}{{"null", nil}, {"true", true}, {"false", false}, {"NaN", json.Number("NaN")}, {"Infinity", json.Number("Infinity")}, {"-Infinity", json.Number("-Infinity")}} {
		if strings.HasPrefix(string(p.data[p.at:]), literal.text) {
			p.at += len(literal.text)
			return literal.value, nil
		}
	}
	token := number.Find(p.data[p.at:])
	if len(token) == 0 {
		return nil, p.failure("Expecting value", p.at)
	}
	p.at += len(token)
	if !strings.ContainsAny(string(token), ".eE") && len(strings.TrimPrefix(string(token), "-")) > 4300 {
		return nil, &BodyError{}
	}
	if strings.ContainsAny(string(token), ".eE") {
		value, _ := strconv.ParseFloat(string(token), 64)
		if math.IsInf(value, 0) {
			if value < 0 {
				return json.Number("-Infinity"), nil
			}
			return json.Number("Infinity"), nil
		}
	}
	return json.Number(token), nil
}

func (p *parser) text() (string, error) {
	start := p.at
	p.at++
	for p.at < len(p.data) {
		c := p.data[p.at]
		if c == '"' {
			p.at++
			var text string
			if err := json.Unmarshal(p.data[start:p.at], &text); err != nil {
				return "", p.failure("Invalid \\uXXXX escape", start)
			}
			return text, nil
		}
		if c < 32 {
			return "", p.failure("Invalid control character at", p.at)
		}
		if c == '\\' {
			slash := p.at
			p.at++
			if p.at == len(p.data) {
				return "", p.failure("Unterminated string starting at", start)
			}
			if !strings.ContainsRune(`"\/bfnrtu`, rune(p.data[p.at])) {
				return "", p.failure("Invalid \\escape", slash)
			}
			if p.data[p.at] == 'u' {
				at := p.at
				if at+4 >= len(p.data) {
					return "", p.failure("Invalid \\uXXXX escape", at)
				}
				if _, err := strconv.ParseUint(string(p.data[at+1:at+5]), 16, 16); err != nil {
					return "", p.failure("Invalid \\uXXXX escape", at)
				}
				p.at += 4
			}
		}
		p.at++
	}
	return "", p.failure("Unterminated string starting at", start)
}

// The Python oracle cannot serialize unpaired surrogate validation input to
// UTF-8. Never silently turn it into U+FFFD and accept a different work item.
func HasUnpairedSurrogate(data []byte) bool {
	inString := false
	for i := 0; i < len(data); i++ {
		if data[i] == '"' {
			inString = !inString
		} else if inString && data[i] == '\\' {
			i++
			if i+4 < len(data) && data[i] == 'u' {
				value, err := strconv.ParseUint(string(data[i+1:i+5]), 16, 16)
				i += 4
				if err != nil || value < 0xd800 || value > 0xdfff {
					continue
				}
				if value >= 0xdc00 || i+6 >= len(data) || data[i+1] != '\\' || data[i+2] != 'u' {
					return true
				}
				low, err := strconv.ParseUint(string(data[i+3:i+7]), 16, 16)
				if err != nil || low < 0xdc00 || low > 0xdfff {
					return true
				}
				i += 6
			}
		}
	}
	return false
}
