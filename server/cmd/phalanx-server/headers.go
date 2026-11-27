package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const incompleteHeadLimit = 16384

type headPhase uint8

const (
	headFields headPhase = iota
	headFixedBody
	headChunkSize
	headChunkData
	headChunkEnd
	headTrailers
	headAwaitIdle
	headNativeRefusal
)

// net/http still parses the original stream. This observer uses native header
// framing only to locate incomplete heads, without routing or rewriting requests.
// Complete oversized heads are not subject to h11's incomplete-event limit.
type headEvents struct {
	phase     headPhase
	fields    []byte
	deferred  []byte
	line      []byte
	remaining uint64
	trailer   int
	last      byte
}

func blankLine(data []byte) int {
	if len(data) > 0 && data[0] == '\n' {
		return 1
	}
	if bytes.HasPrefix(data, []byte("\r\n")) {
		return 2
	}
	for i := 1; i < len(data); i++ {
		if data[i] == '\n' && (data[i-1] == '\n' ||
			i > 1 && data[i-1] == '\r' && data[i-2] == '\n') {
			return i + 1
		}
	}
	return 0
}

func (g *headEvents) nativeRefusal(err error) bool {
	slog.Debug("Incomplete-head observer defers malformed framing to native parser", "error", err)
	g.phase, g.fields, g.line, g.deferred = headNativeRefusal, nil, nil, nil
	return true
}

func (g *headEvents) receive(data []byte) bool {
	for len(data) > 0 {
		switch g.phase {
		case headNativeRefusal:
			return true
		case headAwaitIdle:
			g.deferred = append(g.deferred, data...)
			return true
		case headFields:
			g.fields = append(g.fields, data...)
			// h11 refuses this first byte even before a line is complete.
			// net/http otherwise tolerates leading CR/LF after POST.
			if g.fields[0] < 0x21 {
				return false
			}
			end := blankLine(g.fields)
			if end == 0 {
				return len(g.fields) <= incompleteHeadLimit
			}
			request, err := http.ReadRequest(bufio.NewReader(bytes.NewReader(g.fields[:end])))
			if err != nil {
				return g.nativeRefusal(err)
			}
			data, g.fields = g.fields[end:], nil
			switch {
			case len(request.TransferEncoding) > 0:
				g.phase = headChunkSize
			case request.ContentLength > 0:
				g.phase, g.remaining = headFixedBody, uint64(request.ContentLength)
			default:
				g.phase = headAwaitIdle
			}
		case headFixedBody, headChunkData:
			count := min(uint64(len(data)), g.remaining)
			data, g.remaining = data[count:], g.remaining-count
			if g.remaining == 0 {
				if g.phase == headFixedBody {
					g.phase = headAwaitIdle
				} else {
					g.phase, g.remaining = headChunkEnd, 2
				}
			}
		case headChunkEnd:
			expected := byte('\r')
			if g.remaining == 1 {
				expected = '\n'
			}
			if data[0] != expected {
				return g.nativeRefusal(errors.New("invalid chunk footer"))
			}
			data, g.remaining = data[1:], g.remaining-1
			if g.remaining == 0 {
				g.phase = headChunkSize
			}
		case headChunkSize:
			end := bytes.IndexByte(data, '\n')
			if end == -1 {
				g.line = append(g.line, data...)
				if len(g.line) >= 4096 {
					return g.nativeRefusal(errors.New("native chunk line limit"))
				}
				return true
			}
			g.line = append(g.line, data[:end+1]...)
			line, _, _ := strings.Cut(strings.TrimSpace(string(g.line)), ";")
			size, err := strconv.ParseUint(line, 16, 64)
			if err != nil {
				return g.nativeRefusal(err)
			}
			data, g.line = data[end+1:], nil
			if size == 0 {
				g.phase = headTrailers
			} else {
				g.phase, g.remaining = headChunkData, size
			}
		case headTrailers:
			for len(data) > 0 {
				current := data[0]
				data = data[1:]
				g.trailer++
				if current == '\n' {
					if g.trailer == 1 || g.trailer == 2 && g.last == '\r' {
						g.phase, g.trailer, g.last = headAwaitIdle, 0, 0
						break
					}
					g.trailer = 0
				}
				g.last = current
			}
		}
	}
	return true
}

func (g *headEvents) idle() bool {
	if g.phase != headAwaitIdle {
		return true
	}
	// h11 pauses after a message. All input buffered while paused becomes
	// one receive buffer when the native request/response cycle completes.
	data := g.deferred
	g.deferred, g.phase = nil, headFields
	return g.receive(data)
}

type headReads struct {
	mu         sync.Mutex
	events     headEvents
	pending    []byte
	err        error
	refused    bool
	halfClosed bool
	closeOnce  sync.Once
	closeError error
}

func receiveEventSize() int {
	// Match the pinned asyncio transports, not net/http's 4 KiB bufio reads:
	// Windows ProactorReadPipeTransport vs POSIX SelectorSocketTransport.
	if runtime.GOOS == "windows" {
		return 65536
	}
	return 262144
}

const incompleteHeadResponse = "HTTP/1.1 400 Bad Request\r\ncontent-type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\nInvalid HTTP request received."

func (c *idleConnection) refuseHeadLocked() {
	if _, err := io.WriteString(c.Conn, incompleteHeadResponse); err != nil {
		slog.Error("Cannot send incomplete-header refusal", "error", err)
	}
	if writer, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		if err := writer.CloseWrite(); err != nil {
			slog.Error("Cannot half-close refused transport", "error", err)
		} else {
			c.headers.halfClosed = true
		}
	} else {
		slog.Error("Refused transport does not support a write-side close")
	}
	c.headers.refused, c.headers.pending, c.headers.err = true, nil, io.EOF
	c.headers.events = headEvents{}
}

func (c *idleConnection) Close() error {
	if c.headers == nil {
		return c.Conn.Close()
	}
	g := c.headers
	g.mu.Lock()
	drain := g.refused && g.halfClosed && c.lifetime != nil
	g.mu.Unlock()
	if !drain {
		return c.Conn.Close()
	}
	g.closeOnce.Do(func() {
		// Drain only the finite bytes already queued by the kernel, not future
		// input or peer EOF. This adds no timer, byte cap or open-peer lifetime.
		stop := context.AfterFunc(c.lifetime, func() {
			if err := c.Conn.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
				slog.Error("Cannot interrupt refused transport drain", "error", err)
			}
		})
		defer stop()
		if err := c.Conn.SetReadDeadline(time.Time{}); err != nil && c.lifetime.Err() == nil {
			g.closeError = err
			slog.Error("Cannot clear refused transport read deadline", "error", err)
		} else if available, err := c.queuedRefusedBytes(); err != nil {
			if !errors.Is(err, net.ErrClosed) || c.lifetime.Err() == nil {
				g.closeError = err
				slog.Error("Cannot inspect refused transport queued bytes", "error", err)
			}
		} else if available < 0 {
			g.closeError = errors.New("Refused transport reported negative queued bytes")
			slog.Error("Refused transport reported negative queued bytes")
		} else if _, err := io.CopyN(io.Discard, c.Conn, available); err != nil && c.lifetime.Err() == nil {
			g.closeError = err
			slog.Warn("Refused transport queued-byte drain failed", "error", err)
		}
		err := c.Conn.Close()
		if !errors.Is(err, net.ErrClosed) || c.lifetime.Err() == nil {
			g.closeError = errors.Join(g.closeError, err)
		}
	})
	return g.closeError
}

func (c *idleConnection) queuedRefusedBytes() (int64, error) {
	native, ok := c.Conn.(syscall.Conn)
	if !ok {
		return 0, errors.New("Refused transport has no native socket")
	}
	raw, err := native.SyscallConn()
	if err != nil {
		return 0, err
	}
	var available int64
	var probeError error
	err = raw.Control(func(fd uintptr) { available, probeError = queuedBytes(fd) })
	return available, errors.Join(err, probeError)
}

func (c *idleConnection) readHead(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	g := c.headers
	g.mu.Lock()
	if g.refused {
		g.mu.Unlock()
		return 0, io.EOF
	}
	if len(g.pending) > 0 {
		n := copy(p, g.pending)
		g.pending = g.pending[n:]
		g.mu.Unlock()
		return n, nil
	}
	if g.err != nil {
		err := g.err
		g.err = nil
		g.mu.Unlock()
		return 0, err
	}
	g.mu.Unlock()
	data := make([]byte, receiveEventSize())
	n, err := c.readNetwork(data)
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.refused {
		return 0, io.EOF
	}
	if !g.events.receive(data[:n]) {
		c.refuseHeadLocked()
		return 0, io.EOF
	}
	count := copy(p, data[:n])
	g.pending = data[count:n]
	if count > 0 {
		g.err = err
		return count, nil
	}
	return 0, err
}

func (c *idleConnection) Write(p []byte) (int, error) {
	if c.headers != nil {
		c.headers.mu.Lock()
		refused := c.headers.refused
		c.headers.mu.Unlock()
		if refused {
			return 0, net.ErrClosed
		}
	}
	return c.Conn.Write(p)
}
