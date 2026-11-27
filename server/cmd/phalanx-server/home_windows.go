package main

import (
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/windows"
)

func resolveHome(path string) (string, error) {
	path = filepath.Clean(path)
	var suffix []string
	seen := make(map[string]bool)
	for {
		resolved, err := resolveExistingHome(path)
		if err == nil {
			return appendHomeSuffix(resolved, suffix), nil
		}
		// Python's non-strict Windows resolution also follows dangling
		// reparse points, retaining the target's literal casing/DOS aliases
		// rather than canonicalizing its existing ancestors a second time.
		linked := false
		for !seen[strings.ToLower(path)] {
			seen[strings.ToLower(path)] = true
			target, err := os.Readlink(path)
			if err != nil {
				break
			}
			if !filepath.IsAbs(target) {
				target = filepath.Join(filepath.Dir(path), target)
			}
			linked = true
			path = target
		}
		if linked {
			return appendHomeSuffix(path, suffix), nil
		}
		root := filepath.Clean(path) == filepath.VolumeName(path)+`\`
		parent, tail := filepath.Split(strings.TrimRight(path, `\/`))
		if root || tail == "" || parent == path {
			return appendHomeSuffix(filepath.Clean(path), suffix), nil
		}
		suffix = append(suffix, tail)
		path = parent
	}
}

func appendHomeSuffix(path string, suffix []string) string {
	for i := len(suffix) - 1; i >= 0; i-- {
		if !strings.HasSuffix(path, `\`) {
			path += `\`
		}
		path += suffix[i]
	}
	return path
}

func resolveExistingHome(path string) (string, error) {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	handle, err := windows.CreateFile(name, 0,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil,
		windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(handle)
	buffer := make([]uint16, 512)
	for {
		size, err := windows.GetFinalPathNameByHandle(handle, &buffer[0], uint32(len(buffer)), 0)
		if err != nil {
			return "", err
		}
		if size >= uint32(len(buffer)) {
			buffer = make([]uint16, size+1)
			continue
		}
		resolved := windows.UTF16ToString(buffer[:size])
		if !strings.HasPrefix(path, `\\?\`) {
			if strings.HasPrefix(resolved, `\\?\UNC\`) {
				resolved = `\\` + strings.TrimPrefix(resolved, `\\?\UNC\`)
			} else if len(resolved) > 6 && resolved[5:7] == `:\` {
				resolved = strings.TrimPrefix(resolved, `\\?\`)
			}
		}
		return resolved, nil
	}
}
