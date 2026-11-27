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

func resolveHome(path string) (string, error) {
	// Like Path.resolve(strict=False), resolve existing links before cleaning
	// dot segments on Unix; Windows normalizes segments before resolving handles.
	// Retain a missing/inaccessible suffix without creating or changing anything.
	if runtime.GOOS == "windows" {
		path = filepath.Clean(path)
	}
	var suffix []string
	for {
		resolved, err := resolveExistingHome(path)
		if err == nil {
			for i := len(suffix) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, suffix[i])
				// A missing component followed by .. can return to an existing
				// link; non-strict resolution must resume there.
				if existing, err := resolveExistingHome(resolved); err == nil {
					resolved = existing
				}
			}
			return filepath.Clean(resolved), nil
		}
		separators := string(os.PathSeparator)
		if runtime.GOOS == "windows" {
			separators += "/"
		}
		parent, tail := filepath.Split(strings.TrimRight(path, separators))
		if tail == "" || parent == path || filepath.Dir(path) == filepath.Clean(path) {
			for i := len(suffix) - 1; i >= 0; i-- {
				path = filepath.Join(path, suffix[i])
			}
			return filepath.Clean(path), nil
		}
		suffix = append(suffix, tail)
		path = parent
	}
}
