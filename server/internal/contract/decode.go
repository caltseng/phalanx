package contract

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

// Decode retains exact number tokens for the historical receipt canonicalizer.
// Request syntax and string decoding otherwise follow encoding/json.
func Decode(data []byte) (any, []string, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			err = errors.New("request must contain one JSON value")
		}
		return nil, nil, err
	}
	return value, ObjectKeys(value), nil
}
