package main

import (
	"log/slog"

	"golang.org/x/sys/windows"
)

func foregroundPriority() error {
	process := windows.CurrentProcess()
	priority, err := windows.GetPriorityClass(process)
	if err != nil {
		return err
	}
	if priority == windows.IDLE_PRIORITY_CLASS || priority == windows.BELOW_NORMAL_PRIORITY_CLASS {
		if err := windows.SetPriorityClass(process, windows.NORMAL_PRIORITY_CLASS); err != nil {
			return err
		}
		slog.Warn("Service inherited background CPU priority; changed this process to Normal")
	}
	return nil
}
