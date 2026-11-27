package main

import (
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func testHead(size int, complete bool) string {
	prefix := "GET /health HTTP/1.1\r\nHost: localhost\r\nX-Boundary: "
	result := prefix + strings.Repeat("x", size-len(prefix))
	if complete {
		result += "\r\n\r\n"
	}
	return result
}

func TestIncompleteHeadReceiveEventBoundary(t *testing.T) {
	for _, size := range []int{16383, 16384, 16385, 64000} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			guard := headEvents{}
			if got := guard.receive([]byte(testHead(size, false))); got != (size <= 16384) {
				t.Fatal("incomplete boundary", got)
			}
			guard = headEvents{}
			if !guard.receive([]byte(testHead(size, true))) || guard.phase != headAwaitIdle {
				t.Fatal("complete oversized head was capped")
			}
		})
	}
	guard := headEvents{}
	request := testHead(64000, true)
	if !guard.receive([]byte(request[:16384])) || !guard.receive([]byte(request[16384:])) {
		t.Fatal("completing event was incorrectly capped")
	}
	guard = headEvents{}
	if !guard.receive([]byte(testHead(16384, false))) || guard.receive([]byte("x")) {
		t.Fatal("crossing the boundary without a terminator was not refused")
	}
	for _, ending := range []string{"\r\n\r\n", "\n\n", "\n\r\n"} {
		guard = headEvents{}
		request = testHead(17000, false) + ending
		if !guard.receive([]byte(request)) {
			t.Fatal("complete native line ending refused", ending)
		}
	}
}

func TestPipelinedHeadWaitsForNativeIdle(t *testing.T) {
	for _, complete := range []bool{false, true} {
		guard := headEvents{}
		request := testHead(100, true) + testHead(17000, complete)
		if !guard.receive([]byte(request)) {
			t.Fatal("refusal happened before first native request could complete")
		}
		if guard.idle() != complete {
			t.Fatal("next-head boundary was not evaluated at native idle")
		}
	}
	guard := headEvents{}
	next := testHead(64000, true)
	if !guard.receive([]byte(testHead(100, true)+next[:17000])) ||
		!guard.receive([]byte(next[17000:])) || !guard.idle() {
		t.Fatal("input accumulated while paused was not evaluated together")
	}
}

func TestBodyFramingDoesNotBecomeAHead(t *testing.T) {
	payload := testHead(17000, false) + "\r\n\r\n" + strings.Repeat("y", 17000)
	for _, body := range []struct{ name, header, wire string }{
		{"fixed", fmt.Sprintf("Content-Length: %d\r\n", len(payload)), payload},
		{"chunked", "Transfer-Encoding: chunked\r\n",
			fmt.Sprintf("%x;fixture=yes\r\n%s\r\n1\r\nz\r\n0\r\n\r\n", len(payload), payload)},
		{"trailers", "Transfer-Encoding: chunked\r\n",
			fmt.Sprintf("%x\r\n%s\r\n0\r\nX-Trailer: yes\r\n\r\n", len(payload), payload)},
	} {
		for _, stride := range []int{1, 3, 4096, 65536} {
			t.Run(fmt.Sprintf("%s/%d", body.name, stride), func(t *testing.T) {
				guard := headEvents{}
				wire := "POST / HTTP/1.1\r\nHost: localhost\r\n" + body.header + "\r\n" + body.wire
				for start := 0; start < len(wire); start += stride {
					if !guard.receive([]byte(wire[start:min(start+stride, len(wire))])) {
						t.Fatal("body was subject to head limit")
					}
				}
				if guard.phase != headAwaitIdle {
					t.Fatal("observer did not align with body end", guard.phase)
				}
				if !guard.receive([]byte(testHead(16385, false))) {
					t.Fatal("pipelined input evaluated before native idle")
				}
				if guard.idle() {
					t.Fatal("body skip hid the next incomplete oversized head")
				}
			})
		}
	}
}

func TestMalformedBodyRemainsNativeParserAuthority(t *testing.T) {
	for _, malformed := range []string{"invalid\r\n", "1\r\nx!!", strings.Repeat("f", 4096)} {
		guard := headEvents{}
		request := "POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n" + malformed
		if !guard.receive([]byte(request)) || guard.phase != headNativeRefusal {
			t.Fatal("observer replaced native malformed-body handling", malformed[:min(20, len(malformed))])
		}

	}
	guard := headEvents{}
	if !guard.receive([]byte("POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\nffffffffffffffff\r\n")) ||
		guard.phase != headChunkData || guard.remaining != ^uint64(0) {
		t.Fatal("native uint64 chunk counter was narrowed")
	}
}

func TestHeadReadDoesNotPersistNativeReadAbort(t *testing.T) {
	connection, client := idlePipe(t)
	connection.headers = &headReads{}
	if err := connection.SetReadDeadline(time.Now().Add(-time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Read(make([]byte, 4)); err == nil {
		t.Fatal("native pending-read abort did not interrupt the read")
	}
	if err := connection.SetReadDeadline(time.Time{}); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := client.Write([]byte("GET ")); done <- err }()
	if n, err := connection.Read(make([]byte, 4)); n != 4 || err != nil {
		t.Fatal("transient native read abort poisoned the next request", n, err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestHeadReadAheadRetainsBufferedPrefixExpiry(t *testing.T) {
	connection, client := idlePipe(t)
	connection.headers = &headReads{}
	first := testHead(100, true)
	done := make(chan error, 1)
	go func() { _, err := client.Write([]byte(first + "GET ")); done <- err }()
	if n, err := connection.Read(make([]byte, len(first))); n != len(first) || err != nil {
		t.Fatal(n, err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	idleState(connection, http.StateIdle)
	if err := connection.SetReadDeadline(time.Now().Add(30 * time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	if n, err := connection.Read(make([]byte, 4)); n != 4 || err != nil {
		t.Fatal(n, err)
	}
	if err := connection.SetReadDeadline(time.Time{}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(45 * time.Millisecond)
	_, err := connection.Read(make([]byte, 1))
	if timeout, ok := err.(net.Error); !ok || !timeout.Timeout() {
		t.Fatal("read-ahead was incorrectly counted as new idle input", err)
	}
}

func TestCurrentHeadControlPrefixRefusesBeforeNativePostTolerance(t *testing.T) {
	for _, prefix := range []string{"\r", "\n", "\r\n", " ", "\t"} {
		guard := headEvents{}
		if guard.receive([]byte(prefix)) {
			t.Fatal("initial incomplete invalid request line was accepted", []byte(prefix))
		}
		for _, request := range []string{
			"POST /health HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n",
			"POST /health HTTP/1.1\r\nHost: localhost\r\nContent-Length: 6\r\n\r\n \r\n\t{}",
			"POST /health HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n6\r\n \r\n\t{}\r\n0\r\n\r\n",
		} {
			guard = headEvents{}
			if !guard.receive([]byte(request)) || !guard.idle() || guard.receive([]byte(prefix)) {
				t.Fatal("post-body prefix was not refused at the next head boundary", []byte(prefix))
			}
			guard = headEvents{}
			if !guard.receive([]byte(request+prefix)) || guard.idle() {
				t.Fatal("coalesced prefix was not deferred until native idle", []byte(prefix))
			}
		}
	}
}
