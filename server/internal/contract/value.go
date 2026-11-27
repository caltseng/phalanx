package contract

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"reflect"
	"strconv"
	"strings"
)

// JSONValue projects server-owned values without an encode/decode round trip.
// RawMessage is reserved for JSON read from storage. Canonical remains the
// separate, unchanged request-digest and persistence encoding contract.
func JSONValue(value any) (any, error) {
	return projectJSONValue(value, false)
}

// OrderedJSONValue also retains struct declaration order for Python repr-based
// validators. It is not the HTTP response representation.
func OrderedJSONValue(value any) (any, error) {
	return projectJSONValue(value, true)
}

func projectJSONValue(value any, ordered bool) (any, error) {
	if pointer := reflect.ValueOf(value); pointer.IsValid() && pointer.Kind() == reflect.Pointer && pointer.IsNil() {
		return nil, nil
	}
	switch value := value.(type) {
	case nil, string, bool:
		return value, nil
	case json.RawMessage:
		if value == nil {
			return nil, nil
		}
		decoded, _, err := Decode(value)
		if err != nil {
			return nil, err
		}
		return projectJSONValue(decoded, ordered)
	case json.Number:
		text, err := Canonical(value)
		return json.Number(text), err
	case *orderedObject:
		projected, err := projectJSONValue(value.values, ordered)
		if err != nil {
			return nil, err
		}
		if ordered {
			return &orderedObject{keys: value.keys, values: projected.(map[string]any)}, nil
		}
		return projected, nil
	case interface{ JSONValue() (any, error) }:
		projected, err := value.JSONValue()
		if err != nil {
			return nil, err
		}
		return projectJSONValue(projected, ordered)
	case json.Marshaler:
		return nil, fmt.Errorf("JSON marshaler %T requires an explicit projection", value)
	}
	return reflectJSONValue(reflect.ValueOf(value), ordered)
}

func reflectJSONValue(value reflect.Value, ordered bool) (any, error) {
	switch value.Kind() {
	case reflect.Pointer, reflect.Interface:
		if value.IsNil() {
			return nil, nil
		}
		return projectJSONValue(value.Elem().Interface(), ordered)
	case reflect.String:
		return value.String(), nil
	case reflect.Bool:
		return value.Bool(), nil
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return json.Number(strconv.FormatInt(value.Int(), 10)), nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return json.Number(strconv.FormatUint(value.Uint(), 10)), nil
	case reflect.Float32, reflect.Float64:
		return projectJSONValue(json.Number(strconv.FormatFloat(value.Float(), 'g', -1, value.Type().Bits())), ordered)
	case reflect.Slice:
		if value.IsNil() {
			return nil, nil
		}
		if value.Type().Elem().Kind() == reflect.Uint8 {
			return base64.StdEncoding.EncodeToString(value.Bytes()), nil
		}
		fallthrough
	case reflect.Array:
		result := make([]any, value.Len())
		for index := range result {
			entry, err := projectJSONValue(value.Index(index).Interface(), ordered)
			if err != nil {
				return nil, err
			}
			result[index] = entry
		}
		return result, nil
	case reflect.Map:
		if value.IsNil() {
			return nil, nil
		}
		if value.Type().Key().Kind() != reflect.String {
			return nil, fmt.Errorf("unsupported JSON map key %s", value.Type().Key())
		}
		result := make(map[string]any, value.Len())
		entries := value.MapRange()
		for entries.Next() {
			entry, err := projectJSONValue(entries.Value().Interface(), ordered)
			if err != nil {
				return nil, err
			}
			result[entries.Key().String()] = entry
		}
		return result, nil
	case reflect.Struct:
		result := map[string]any{}
		keys := []string{}
		for index := 0; index < value.NumField(); index++ {
			field := value.Type().Field(index)
			if !field.IsExported() {
				continue
			}
			tag := field.Tag.Get("json")
			name, options, _ := strings.Cut(tag, ",")
			if name == "-" {
				continue
			}
			entry := value.Field(index)
			if strings.Contains(","+options+",", ",omitempty,") && emptyJSONValue(entry) {
				continue
			}
			if field.Anonymous && name == "" {
				embedded, err := projectJSONValue(entry.Interface(), ordered)
				if err != nil {
					return nil, err
				}
				if embedded == nil {
					continue
				}
				fields, ok := ObjectValues(embedded)
				if !ok {
					return nil, fmt.Errorf("embedded JSON field %s is not an object", field.Name)
				}
				for _, key := range ObjectKeys(embedded) {
					if _, exists := result[key]; exists {
						return nil, fmt.Errorf("ambiguous JSON field %s requires an explicit projection", key)
					}
					result[key] = fields[key]
					keys = append(keys, key)
				}
				continue
			}
			if name == "" {
				name = field.Name
			}
			if _, exists := result[name]; exists {
				return nil, fmt.Errorf("ambiguous JSON field %s requires an explicit projection", name)
			}
			projected, err := projectJSONValue(entry.Interface(), ordered)
			if err != nil {
				return nil, err
			}
			result[name] = projected
			keys = append(keys, name)
		}
		if ordered {
			return &orderedObject{keys: keys, values: result}, nil
		}
		return result, nil
	default:
		return nil, fmt.Errorf("unsupported JSON value %s", value.Type())
	}
}

func emptyJSONValue(value reflect.Value) bool {
	switch value.Kind() {
	case reflect.Array, reflect.Map, reflect.Slice, reflect.String:
		return value.Len() == 0
	case reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64,
		reflect.Float32, reflect.Float64, reflect.Interface, reflect.Pointer:
		return value.IsZero()
	}
	return false
}
