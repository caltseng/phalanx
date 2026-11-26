package sqlite

import (
	"errors"
	"strings"

	driver "modernc.org/sqlite"
	sqlite3 "modernc.org/sqlite/lib"
)

func IsDriverError(err error) bool {
	var failure *driver.Error
	return errors.As(err, &failure)
}

func FailureName(err error) (string, bool) {
	var failure *driver.Error
	if !errors.As(err, &failure) {
		return "", false
	}
	extended := map[int]string{
		sqlite3.SQLITE_CONSTRAINT_CHECK:      "SQLITE_CONSTRAINT_CHECK",
		sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY: "SQLITE_CONSTRAINT_FOREIGNKEY",
		sqlite3.SQLITE_CONSTRAINT_NOTNULL:    "SQLITE_CONSTRAINT_NOTNULL",
		sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY: "SQLITE_CONSTRAINT_PRIMARYKEY",
		sqlite3.SQLITE_CONSTRAINT_TRIGGER:    "SQLITE_CONSTRAINT_TRIGGER",
		sqlite3.SQLITE_CONSTRAINT_UNIQUE:     "SQLITE_CONSTRAINT_UNIQUE",
		sqlite3.SQLITE_CONSTRAINT_DATATYPE:   "SQLITE_CONSTRAINT_DATATYPE",
	}
	if name, ok := extended[failure.Code()]; ok {
		return name, true
	}
	description, ok := driver.ErrorCodeString[failure.Code()]
	if !ok {
		return "", false
	}
	start, end := strings.LastIndexByte(description, '('), strings.LastIndexByte(description, ')')
	if start < 0 || end <= start {
		return "", false
	}
	return description[start+1 : end], true
}
