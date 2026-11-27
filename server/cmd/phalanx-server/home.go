package main

import (
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"strings"
)

func configuredHome(home string, lookup func(string) (string, bool)) (*string, error) {
	if strings.HasPrefix(home, "~") {
		separators := "/"
		if runtime.GOOS == "windows" {
			separators += `\`
		}
		end := strings.IndexAny(home, separators)
		if end < 0 {
			end = len(home)
		}
		name := home[1:end]
		var directory string
		var found bool
		if runtime.GOOS == "windows" {
			directory, found = lookup("USERPROFILE")
			if !found {
				if path, ok := lookup("HOMEPATH"); ok {
					drive, _ := lookup("HOMEDRIVE")
					directory, found = drive+path, true
				}
			}
			if name != "" {
				username, _ := lookup("USERNAME")
				if name != username && filepath.Base(directory) != username {
					found = false
				} else if name != username {
					directory = filepath.Join(filepath.Dir(directory), name)
				}
			}
		} else if name == "" {
			directory, found = lookup("HOME")
			if !found {
				if current, err := user.Current(); err == nil {
					directory, found = current.HomeDir, true
				}
			}
		} else if account, err := user.Lookup(name); err == nil {
			directory, found = account.HomeDir, true
		}
		if found {
			home = strings.TrimRight(directory, string(os.PathSeparator)) + home[end:]
			if home == "" && runtime.GOOS != "windows" {
				home = "/"
			}
		}
	}
	if !filepath.IsAbs(home) {
		return nil, nil
	}
	resolved, err := resolveHome(home)
	if err != nil {
		return nil, err
	}
	return &resolved, nil
}
