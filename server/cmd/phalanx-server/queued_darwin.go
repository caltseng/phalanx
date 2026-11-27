//go:build darwin

package main

import "golang.org/x/sys/unix"

func queuedBytes(fd uintptr) (int64, error) {
	const fionread = 0x4004667f
	available, err := unix.IoctlGetInt(int(fd), fionread)
	return int64(available), err
}
