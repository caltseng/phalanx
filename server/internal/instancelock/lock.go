// Package instancelock interoperates with deployment.run_service's Python lock.
package instancelock

import (
	"os"
	"path/filepath"
)

func Acquire(database string) (*os.File, error) {
	path, err := filepath.Abs(database)
	if err != nil {
		return nil, err
	}
	path, err = filepath.EvalSymlinks(path)
	if err != nil {
		return nil, err
	}
	handle, err := os.OpenFile(path+".service.lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := lock(handle); err != nil {
		handle.Close()
		return nil, err
	}
	return handle, nil
}
