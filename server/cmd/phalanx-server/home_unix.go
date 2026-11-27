//go:build !windows

package main

import (
	"os"
	"path/filepath"
	"strings"
)

func resolveExistingHome(path string) (string, error) {
	return filepath.EvalSymlinks(path)
}

func resolveHome(path string) (string, error) {
	type part struct{ name, link string }
	var pending []part
	push := func(path string) {
		names := strings.Split(path, "/")
		for i := len(names) - 1; i >= 0; i-- {
			pending = append(pending, part{name: names[i]})
		}
	}
	push(path)
	resolved := "/"
	seen := make(map[string]string)
	for len(pending) > 0 {
		next := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		if next.link != "" {
			seen[next.link] = resolved
			continue
		}
		switch next.name {
		case "", ".":
			continue
		case "..":
			resolved = filepath.Dir(resolved)
			continue
		}
		candidate := filepath.Join(resolved, next.name)
		info, err := os.Lstat(candidate)
		// Path.resolve(strict=False) retains missing/inaccessible components,
		// but still follows later links and links to non-existent targets.
		if err != nil || info.Mode()&os.ModeSymlink == 0 {
			resolved = candidate
			continue
		}
		if cached, present := seen[candidate]; present {
			resolved = candidate
			if cached != "" {
				resolved = cached
			}
			continue
		}
		target, err := os.Readlink(candidate)
		if err != nil {
			resolved = candidate
			continue
		}
		seen[candidate] = ""
		pending = append(pending, part{link: candidate})
		if filepath.IsAbs(target) {
			resolved = "/"
		}
		push(target)
	}
	return resolved, nil
}
