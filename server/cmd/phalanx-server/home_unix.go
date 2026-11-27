//go:build !windows

package main

import "path/filepath"

func resolveExistingHome(path string) (string, error) {
	return filepath.EvalSymlinks(path)
}
