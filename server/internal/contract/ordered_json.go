package contract

import (
	"bytes"
	"encoding/json"
)

// PythonJSON preserves declared/decoded key order and json.dumps' default
// separators for storage contracts that do not use intake's canonical encoder.
func PythonJSON(value any) (string, error) {
	var buffer bytes.Buffer
	err := writePythonValue(&buffer, value)
	return buffer.String(), err
}

func writePythonValue(buffer *bytes.Buffer, value any) error {
	switch value := value.(type) {
	case nil, string, bool, json.Number:
		return writeValue(buffer, value)
	case *orderedObject:
		return writePythonObject(buffer, value.values, value.keys)
	case map[string]any:
		if value == nil {
			return writeValue(buffer, nil)
		}
		return writePythonObject(buffer, value, ObjectKeys(value))
	case []any:
		if value == nil {
			return writeValue(buffer, nil)
		}
		buffer.WriteByte('[')
		for index, entry := range value {
			if index > 0 {
				buffer.WriteString(", ")
			}
			if err := writePythonValue(buffer, entry); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
		return nil
	default:
		data, err := json.Marshal(value)
		if err != nil {
			return err
		}
		decoded, _, err := Decode(data)
		if err != nil {
			return err
		}
		return writePythonValue(buffer, decoded)
	}
}

func writePythonObject(buffer *bytes.Buffer, values map[string]any, keys []string) error {
	buffer.WriteByte('{')
	for index, key := range keys {
		if index > 0 {
			buffer.WriteString(", ")
		}
		writeString(buffer, key)
		buffer.WriteString(": ")
		if err := writePythonValue(buffer, values[key]); err != nil {
			return err
		}
	}
	buffer.WriteByte('}')
	return nil
}
