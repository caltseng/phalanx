package application

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

var titleLineBreak = regexp.MustCompile("[\n\r\v\f\u0085\u2028\u2029]")

func shortTitle(goal string, limit int) string {
	line := ""
	for _, value := range titleLineBreak.Split(goal, -1) {
		value = strings.TrimSpace(value)
		if value != "" {
			line = strings.TrimSpace(strings.TrimLeft(value, "#*-> "))
			break
		}
	}
	sentence, previous := line, rune(0)
	for index, value := range line {
		if (previous == '.' || previous == '!' || previous == '?') && unicode.IsSpace(value) {
			sentence = line[:index]
			break
		}
		previous = value
	}
	sentence = strings.TrimSpace(sentence)
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
	if strings.Contains(strings.TrimSpace(cut), " ") {
		trimmed := strings.TrimRightFunc(cut, unicode.IsSpace)
		last := -1
		for index, value := range trimmed {
			if unicode.IsSpace(value) {
				last = index
			}
		}
		if last >= 0 {
			cut = strings.TrimRightFunc(trimmed[:last], unicode.IsSpace)
		}
	}
	return strings.TrimRight(cut, " ,;:-") + "\u2026"
}

var displayedSecret = regexp.MustCompile(`(^|[^\p{L}\p{N}_])((?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|(?i:(?:bearer|basic) +[^ ]+|(?:[a-z][a-z0-9]*[_-])*(?:token|secret|password|lease|(?:api|secret)[_-]?key)["']? *[:=] *(?:"[^"]*"|'[^']*'|[^ ]+)))`)

func reasonText(value string, limit int) string {
	value = strings.Join(strings.Fields(value), " ")
	value = displayedSecret.ReplaceAllStringFunc(value, func(match string) string {
		return displayedSecret.FindStringSubmatch(match)[1] + "[redacted]"
	})
	if utf8.RuneCountInString(value) > limit {
		value = firstRunes(value, limit-1) + "\u2026"
	}
	return value
}
