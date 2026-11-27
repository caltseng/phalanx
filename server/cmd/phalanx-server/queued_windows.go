//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

func queuedBytes(fd uintptr) (int64, error) {
	const fionread = 0x4004667f
	var available, returned uint32
	err := syscall.WSAIoctl(syscall.Handle(fd), fionread, nil, 0,
		(*byte)(unsafe.Pointer(&available)), uint32(unsafe.Sizeof(available)), &returned, nil, 0)
	if err == nil && returned != uint32(unsafe.Sizeof(available)) {
		err = fmt.Errorf("Winsock queued-byte probe returned %d bytes", returned)
	}
	return int64(available), err
}
