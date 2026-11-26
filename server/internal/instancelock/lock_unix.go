//go:build linux || darwin

package instancelock

import (
	"os"

	"golang.org/x/sys/unix"
)

func lock(handle *os.File) error {
	return unix.Flock(int(handle.Fd()), unix.LOCK_EX|unix.LOCK_NB)
}
