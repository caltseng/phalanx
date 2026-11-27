package application

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

func pythonWhitespace(value rune) bool {
	return unicode.IsSpace(value) || value >= 0x1c && value <= 0x1f
}

func pythonLines(value string) []string {
	return strings.FieldsFunc(value, func(value rune) bool {
		return value == '\n' || value == '\r' || value == '\v' || value == '\f' ||
			value >= 0x1c && value <= 0x1e || value == 0x85 || value == 0x2028 || value == 0x2029
	})
}

func shortTitle(goal string, limit int) string {
	line := ""
	for _, value := range pythonLines(goal) {
		value = strings.TrimFunc(value, pythonWhitespace)
		if value != "" {
			line = strings.TrimFunc(strings.TrimLeft(value, "#*-> "), pythonWhitespace)
			break
		}
	}
	sentence, previous := line, rune(0)
	for index, value := range line {
		if (previous == '.' || previous == '!' || previous == '?') && pythonWhitespace(value) {
			sentence = line[:index]
			break
		}
		previous = value
	}
	sentence = strings.TrimFunc(sentence, pythonWhitespace)
	if sentence == "" {
		sentence = line
	}
	if sentence == "" {
		sentence = "Untitled"
	}
	if utf8.RuneCountInString(sentence) <= limit {
		return sentence
	}
	cut := firstRunes(sentence, limit-1)
	if strings.Contains(strings.TrimFunc(cut, pythonWhitespace), " ") {
		trimmed := strings.TrimRightFunc(cut, pythonWhitespace)
		last := -1
		for index, value := range trimmed {
			if pythonWhitespace(value) {
				last = index
			}
		}
		if last >= 0 {
			cut = strings.TrimRightFunc(trimmed[:last], pythonWhitespace)
		}
	}
	return strings.TrimRight(cut, " ,;:-") + "\u2026"
}

var displayedToken = regexp.MustCompile(`(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+`)
var displayedAuthorization = regexp.MustCompile(`(?:bearer|basic) +[^ ]+`)
var displayedAssignment = regexp.MustCompile(`(?:[a-z][a-z0-9]*[_-])*(?:token|secret|password|lease|(?:api|secret)[_-]?key)["']? *[:=] *(?:"[^"]*"|'[^']*'|[^ ]+)`)

type redactedSpan struct{ start, end, order int }

func wordRune(value rune) bool {
	return unicode.IsLetter(value) || unicode.IsNumber(value) || value == '_'
}

func reasonText(value string, limit int) string {
	value = strings.Join(strings.FieldsFunc(value, pythonWhitespace), " ")
	folded := pythonLowerASCII(value)
	boundaries := []int{}
	for index := range value {
		boundaries = append(boundaries, index)
	}
	boundaries = append(boundaries, len(value))
	foldedBoundaries := []int{}
	for index := range folded {
		foldedBoundaries = append(foldedBoundaries, index)
	}
	foldedBoundaries = append(foldedBoundaries, len(folded))
	mapBoundary := func(index int) int {
		return boundaries[sort.SearchInts(foldedBoundaries, index)]
	}
	spans := []redactedSpan{}
	for order, expression := range []*regexp.Regexp{displayedToken, displayedAuthorization, displayedAssignment} {
		source := value
		if order != 0 {
			source = folded
		}
		for _, found := range expression.FindAllStringIndex(source, -1) {
			start, end := found[0], found[1]
			if order != 0 {
				start, end = mapBoundary(start), mapBoundary(end)
			}
			if start > 0 {
				before, _ := utf8.DecodeLastRuneInString(value[:start])
				if wordRune(before) {
					continue
				}
			}
			spans = append(spans, redactedSpan{start, end, order})
		}
	}
	sort.SliceStable(spans, func(i, j int) bool {
		return spans[i].start < spans[j].start || spans[i].start == spans[j].start && spans[i].order < spans[j].order
	})
	var text strings.Builder
	end := 0
	for _, span := range spans {
		if span.start < end {
			continue
		}
		text.WriteString(value[end:span.start])
		text.WriteString("[redacted]")
		end = span.end
	}
	text.WriteString(value[end:])
	result := text.String()
	if utf8.RuneCountInString(result) > limit {
		result = firstRunes(result, limit-1) + "\u2026"
	}
	return result
}
