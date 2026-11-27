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
			for i := len(suffix) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, suffix[i])
			}
			return resolved, nil
		}
		// Python's non-strict Windows resolution also follows dangling
		// reparse points before preserving the missing suffix.
		if target, err := os.Readlink(path); err == nil && !seen[path] {
			seen[path] = true
			if !filepath.IsAbs(target) {
				target = filepath.Join(filepath.Dir(path), target)
			}
			path = target
			continue
		}
		root := filepath.Clean(path) == filepath.VolumeName(path)+`\`
		parent, tail := filepath.Split(strings.TrimRight(path, `\/`))
		if root || tail == "" || parent == path {
			for i := len(suffix) - 1; i >= 0; i-- {
				path = filepath.Join(path, suffix[i])
			}
			return filepath.Clean(path), nil
		}
		suffix = append(suffix, tail)
		path = parent
	}
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
