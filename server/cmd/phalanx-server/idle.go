package main

import (
	"errors"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"
)

// net/http waits for four bytes before leaving idle. Python leaves idle on
// the first new byte; buffered prefixes alone do not count as new activity.
type idleConnection struct {
	net.Conn
	mu       sync.Mutex
	idle     bool
	received bool
	deadline time.Time
}

func (c *idleConnection) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if n > 0 {
		c.mu.Lock()
		if c.idle {
			c.received = true
			if !c.deadline.IsZero() {
				err = errors.Join(err, c.Conn.SetReadDeadline(time.Time{}))
				c.deadline = time.Time{}
			}
		}
		c.mu.Unlock()
	}
	return n, err
}

func (c *idleConnection) SetReadDeadline(deadline time.Time) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.idle {
		switch {
		case deadline.After(time.Now()):
			if c.received {
				deadline = time.Time{}
			}
			c.deadline = deadline
		case deadline.IsZero() && !c.deadline.IsZero() && !c.received:
			// Peek(4) can complete from previously buffered bytes. Keep the
			// existing idle deadline until new input or a complete request.
			deadline = c.deadline
		default:
			c.deadline = time.Time{}
		}
	}
	return c.Conn.SetReadDeadline(deadline)
}

func (c *idleConnection) state(state http.ConnState) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if state == http.StateIdle {
		c.idle, c.received = true, false
		return nil
	}
	c.idle, c.received = false, false
	if state == http.StateActive && !c.deadline.IsZero() {
		c.deadline = time.Time{}
		return c.Conn.SetReadDeadline(time.Time{})
	}
	c.deadline = time.Time{}
	return nil
}

type idleListener struct{ net.Listener }

func (l idleListener) Accept() (net.Conn, error) {
	connection, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &idleConnection{Conn: connection}, nil
}

func idleState(connection net.Conn, state http.ConnState) {
	if err := connection.(*idleConnection).state(state); err != nil {
		slog.Error("Cannot preserve connection idle deadline", "error", err)
		if closeError := connection.Close(); closeError != nil {
			slog.Error("Cannot close failed idle connection", "error", closeError)
		}
	}
}
