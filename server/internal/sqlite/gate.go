package sqlite

import (
	"container/list"
	"context"
	"sync"
	"time"
)

const AdmissionTimeout = 4 * time.Second

type gate struct {
	mu          sync.Mutex
	changed     chan struct{}
	writing     bool
	writers     int
	writerQueue list.List
	admitted    int
	queued      int
	writes      uint64
	timeout     time.Duration
}

func newGate() *gate {
	return &gate{changed: make(chan struct{}), timeout: AdmissionTimeout}
}

func (g *gate) notify() {
	close(g.changed)
	g.changed = make(chan struct{})
}

func (g *gate) wait(ctx context.Context, read bool, ready func() bool) error {
	timer := time.NewTimer(g.timeout)
	defer timer.Stop()
	for {
		// An explicit operation interruption takes precedence over admission,
		// including after a writer transferred a queued reader into its batch.
		if err := ctx.Err(); err != nil {
			return err
		}
		if ready() {
			return nil
		}
		changed := g.changed
		g.mu.Unlock()
		var err error
		select {
		case <-changed:
		case <-ctx.Done():
			err = ctx.Err()
		case <-timer.C:
			err = &AdmissionError{Read: read}
		}
		g.mu.Lock()
		if err != nil {
			return err
		}
	}
}

func (g *gate) write(ctx context.Context) (func(), error) {
	g.mu.Lock()
	g.writers++
	turn := g.writerQueue.PushBack(nil)
	err := g.wait(ctx, false, func() bool {
		return g.writerQueue.Front() == turn && !g.writing && g.admitted == 0
	})
	g.writerQueue.Remove(turn)
	g.writers--
	if err != nil {
		g.notify()
		g.mu.Unlock()
		return nil, err
	}
	g.writing = true
	g.mu.Unlock()
	return func() {
		g.mu.Lock()
		defer g.mu.Unlock()
		g.writing = false
		g.writes++
		g.admitted += g.queued
		g.queued = 0
		g.notify()
	}, nil
}

func (g *gate) readStart(ctx context.Context) (func(), error) {
	g.mu.Lock()
	if g.writing || g.writers > 0 {
		g.queued++
		writes := g.writes
		err := g.wait(ctx, true, func() bool {
			return g.writes != writes || !g.writing && g.writers == 0
		})
		if err != nil {
			if g.writes == writes {
				g.queued--
			} else {
				g.admitted--
			}
			g.notify()
			g.mu.Unlock()
			return nil, err
		}
		if g.writes == writes {
			g.queued--
			g.admitted++
		}
	} else {
		if err := ctx.Err(); err != nil {
			g.mu.Unlock()
			return nil, err
		}
		g.admitted++
	}
	g.mu.Unlock()
	return func() {
		g.mu.Lock()
		defer g.mu.Unlock()
		g.admitted--
		g.notify()
	}, nil
}
