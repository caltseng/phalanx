package instancelock

import (
	"os"

	"golang.org/x/sys/windows"
)

func lock(handle *os.File) error {
	return windows.LockFileEx(windows.Handle(handle.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, &windows.Overlapped{})
}
