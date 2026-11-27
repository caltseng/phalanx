package contract

import (
	"encoding/json"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type AwareTime struct {
	time.Time
}

func (value AwareTime) MarshalJSON() ([]byte, error) {
	return dateTimeJSON(value.Time, true)
}

func (value AwareTime) JSONValue() (any, error) {
	return dateTimeText(value.Time, true), nil
}

type DateTime struct {
	time.Time
	Aware bool
}

func (value DateTime) MarshalJSON() ([]byte, error) {
	return dateTimeJSON(value.Time, value.Aware)
}

func (value DateTime) JSONValue() (any, error) {
	return dateTimeText(value.Time, value.Aware), nil
}

func dateTimeJSON(value time.Time, aware bool) ([]byte, error) {
	return json.Marshal(dateTimeText(value, aware))
}

func dateTimeText(value time.Time, aware bool) string {
	layout := "2006-01-02T15:04:05"
	if value.Nanosecond() != 0 {
		layout += ".000000"
	}
	if aware {
		layout += "Z07:00"
	}
	return value.Format(layout)
}

var datetimeNumber = regexp.MustCompile(`^[+-]?(?:[0-9]+|(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)$`)
var datetimeClock = regexp.MustCompile(`^[Tt _]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]+))?)?([Zz]|[+-][0-9]{2}:?[0-9]{2})?$`)

func ValidateAwareTime(value any, at []any) (AwareTime, []Issue) {
	v := validator{}
	result := v.awareTime(value, at)
	return result, v.issues
}

func ValidateDateTime(value any, at []any) (DateTime, []Issue) {
	v := validator{}
	result := v.dateTime(value, at)
	return result, v.issues
}

func (v *validator) datetimeError(value any, at []any, reason string, fromDate bool) DateTime {
	kind, message := "datetime_parsing", "Input should be a valid datetime, "
	if fromDate {
		kind, message = "datetime_from_date_parsing", "Input should be a valid datetime or date, "
	}
	v.issue(kind, message+reason, at, value, map[string]any{"error": reason})
	return DateTime{}
}

func (v *validator) awareTime(value any, at []any) AwareTime {
	start := len(v.issues)
	result := v.dateTime(value, at)
	if len(v.issues) == start && !result.Aware {
		v.issue("timezone_aware", "Input should have timezone info", at, value, nil)
	}
	return AwareTime{result.Time}
}

func (v *validator) dateTime(value any, at []any) DateTime {
	text, isText := value.(string)
	number, isNumber := value.(json.Number)
	if isNumber || isText && datetimeNumber.MatchString(text) {
		if isNumber {
			text = string(number)
		}
		seconds, err := strconv.ParseFloat(text, 64)
		if math.IsNaN(seconds) {
			return v.datetimeError(value, at, "NaN values not permitted", false)
		}
		if math.IsInf(seconds, -1) {
			return v.datetimeError(value, at, "dates before 0000 are not supported as unix timestamps", isText)
		}
		if math.IsInf(seconds, 1) {
			return v.datetimeError(value, at, "dates after 9999 are not supported as unix timestamps", isText)
		}
		if err != nil && !math.IsInf(seconds, 0) {
			return v.datetimeError(value, at, "dates after 9999 are not supported as unix timestamps", false)
		}
		whole := math.Floor(seconds)
		fraction := seconds - whole
		if math.Abs(seconds) > 20000000000 {
			scaled := math.Floor(whole / 1000)
			fraction = ((whole - scaled*1000) + fraction) / 1000
			whole = scaled
		}
		seconds = whole + fraction
		if seconds < -62135596800 {
			if seconds >= -62167219200 {
				return v.datetimeError(value, at, "year 0 is out of range", false)
			}
			return v.datetimeError(value, at, "dates before 0000 are not supported as unix timestamps", isText)
		}
		if seconds >= 253402300800 {
			return v.datetimeError(value, at, "dates after 9999 are not supported as unix timestamps", isText)
		}
		microseconds := int64(math.Floor(fraction*1000000 + 0.5))
		result := time.Unix(int64(whole), microseconds*1000).UTC()
		if result.Year() > 9999 {
			return v.datetimeError(value, at, "dates after 9999 are not supported as unix timestamps", false)
		}
		return DateTime{Time: result, Aware: true}
	}
	if !isText {
		v.issue("datetime_type", "Input should be a valid datetime", at, value, nil)
		return DateTime{}
	}
	if len(text) < 10 {
		return v.datetimeError(value, at, "input is too short", true)
	}
	digits := func(start, end int) (int, bool) {
		for _, digit := range text[start:end] {
			if digit < '0' || digit > '9' {
				return 0, false
			}
		}
		number, err := strconv.Atoi(text[start:end])
		return number, err == nil
	}
	year, ok := digits(0, 4)
	if !ok {
		return v.datetimeError(value, at, "invalid character in year", true)
	}
	if text[4] != '-' {
		return v.datetimeError(value, at, "invalid date separator, expected `-`", true)
	}
	month, ok := digits(5, 7)
	if !ok {
		return v.datetimeError(value, at, "invalid character in month", true)
	}
	if text[7] != '-' {
		return v.datetimeError(value, at, "invalid date separator, expected `-`", true)
	}
	day, ok := digits(8, 10)
	if !ok {
		return v.datetimeError(value, at, "invalid character in day", true)
	}
	if month < 1 || month > 12 {
		return v.datetimeError(value, at, "month value is outside expected range of 1-12", true)
	}
	date := time.Date(year, time.Month(month), day, 0, 0, 0, 0, time.UTC)
	if day < 1 || date.Month() != time.Month(month) {
		return v.datetimeError(value, at, "day value is outside expected range", true)
	}
	if len(text) == 10 {
		if year == 0 {
			return v.datetimeError(value, at, "year 0 is out of range", false)
		}
		return DateTime{Time: date}
	}
	clock := datetimeClock.FindStringSubmatch(text[10:])
	extra := "unexpected extra characters at the end of the input"
	if clock == nil {
		return v.datetimeError(value, at, extra, true)
	}
	part := func(index int) int {
		number, _ := strconv.Atoi(clock[index])
		return number
	}
	hour, minute, second := part(1), part(2), part(3)
	if hour > 23 || minute > 59 || second > 59 {
		return v.datetimeError(value, at, extra, true)
	}
	fraction := clock[4]
	if len(fraction) > 6 {
		fraction = fraction[:6]
	}
	microseconds := 0
	if fraction != "" {
		microseconds, _ = strconv.Atoi(fraction + strings.Repeat("0", 6-len(fraction)))
	}
	zone := clock[5]
	offset := 0
	if len(zone) > 1 {
		zoneDigits := strings.ReplaceAll(zone[1:], ":", "")
		hours, _ := strconv.Atoi(zoneDigits[:2])
		minutes, _ := strconv.Atoi(zoneDigits[2:])
		if hours > 23 || minutes > 59 {
			return v.datetimeError(value, at, extra, true)
		}
		offset = (hours*60 + minutes) * 60
		if zone[0] == '-' {
			offset = -offset
		}
	}
	if year == 0 {
		return v.datetimeError(value, at, "year 0 is out of range", false)
	}
	return DateTime{Time: time.Date(year, time.Month(month), day, hour, minute, second, microseconds*1000,
		time.FixedZone("", offset)), Aware: zone != ""}
}
