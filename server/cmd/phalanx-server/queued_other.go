//go:build !windows && !linux && !darwin

package main

import "errors"

func queuedBytes(_ uintptr) (int64, error) {
	return 0, errors.New("Refused transport queued-byte probe is unsupported on this platform")
}
