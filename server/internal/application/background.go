package application

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// Existing durable jobs run independently of writer claims. Stop prevents the
// next batch; a dispatched batch drains before its caller releases the lock.
func (a *App) RunBackground(ctx context.Context, logger *slog.Logger) {
	var workers sync.WaitGroup
	workers.Add(2)
	go func() {
		defer workers.Done()
		a.backgroundLoop(ctx, logger, "report finalization", time.Second,
			func(operation context.Context) (time.Duration, error) {
				return time.Second, a.ProcessFinalizations(operation)
			})
	}()
	go func() {
		defer workers.Done()
		a.backgroundLoop(ctx, logger, "dependency refresh", 500*time.Millisecond,
			func(operation context.Context) (time.Duration, error) {
				worked, err := a.ProcessRefresh(operation)
				if worked {
					return 50 * time.Millisecond, err
				}
				return 500 * time.Millisecond, err
			})
	}()
	workers.Wait()
}

func (a *App) backgroundLoop(ctx context.Context, logger *slog.Logger, name string, retryDelay time.Duration,
	process func(context.Context) (time.Duration, error)) {
	for {
		if ctx.Err() != nil {
			return
		}
		delay, err := process(context.WithoutCancel(ctx))
		if err != nil {
			logger.Error("Durable background operation failed", "operation", name, "error", err)
			delay = retryDelay
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
