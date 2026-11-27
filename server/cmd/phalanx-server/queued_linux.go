//go:build linux

package main

import "golang.org/x/sys/unix"

func queuedBytes(fd uintptr) (int64, error) {
	available, err := unix.IoctlGetInt(int(fd), unix.TIOCINQ)
	return int64(available), err
}
