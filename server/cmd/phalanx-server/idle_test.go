package main

import (
	"net"
	"net/http"
	"testing"
	"time"
)

func idlePipe(t *testing.T) (*idleConnection, net.Conn) {
	t.Helper()
	server, client := net.Pipe()
	t.Cleanup(func() { server.Close(); client.Close() })
	return &idleConnection{Conn: server}, client
}

func TestFirstIdleByteCancelsDeadlineAcrossRepeatedRequests(t *testing.T) {
	connection, client := idlePipe(t)
	for index := 0; index < 8; index++ {
		if err := connection.state(http.StateIdle); err != nil {
			t.Fatal(err)
		}
		if err := connection.SetReadDeadline(time.Now().Add(30 * time.Millisecond)); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() { _, err := client.Write([]byte("G")); done <- err }()
		buffer := make([]byte, 4)
		if n, err := connection.Read(buffer); n != 1 || err != nil {
			t.Fatal(n, err)
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
		time.Sleep(45 * time.Millisecond)
		go func() { _, err := client.Write([]byte("ET ")); done <- err }()
		if n, err := connection.Read(buffer); n != 3 || err != nil {
			t.Fatal("idle byte imposed a header deadline", n, err)
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
		if err := connection.state(http.StateActive); err != nil {
			t.Fatal(err)
		}
	}
}

func TestNoNewInputRetainsIdleDeadlineAndPastAbort(t *testing.T) {
	for _, buffered := range []bool{false, true} {
		t.Run(map[bool]string{false: "no-input", true: "buffered-prefix"}[buffered], func(t *testing.T) {
			connection, _ := idlePipe(t)
			if err := connection.state(http.StateIdle); err != nil {
				t.Fatal(err)
			}
			if err := connection.SetReadDeadline(time.Now().Add(30 * time.Millisecond)); err != nil {
				t.Fatal(err)
			}
			if buffered {
				if err := connection.SetReadDeadline(time.Time{}); err != nil {
					t.Fatal(err)
				}
			}
			_, err := connection.Read(make([]byte, 1))
			if timeout, ok := err.(net.Error); !ok || !timeout.Timeout() {
				t.Fatal("genuine idle expiry was removed", err)
			}
			if err := connection.SetReadDeadline(time.Now().Add(-time.Second)); err != nil {
				t.Fatal(err)
			}
			if _, err := connection.Read(make([]byte, 1)); err == nil {
				t.Fatal("past read-abort deadline was swallowed")
			}
		})
	}
}
