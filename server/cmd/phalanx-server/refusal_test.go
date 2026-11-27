package main

import (
	"context"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func refusedTCP(t *testing.T) (*idleConnection, *net.TCPConn, context.CancelFunc) {
	t.Helper()
	listener, err := net.ListenTCP("tcp", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	client, err := net.DialTCP("tcp", nil, listener.Addr().(*net.TCPAddr))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := listener.AcceptTCP()
	if err != nil {
		client.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	connection := &idleConnection{Conn: raw, headers: &headReads{}, lifetime: ctx}
	t.Cleanup(func() { cancel(); client.Close(); connection.Close() })
	if err := client.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	connection.headers.mu.Lock()
	connection.refuseHeadLocked()
	connection.headers.mu.Unlock()
	return connection, client, cancel
}

func TestRefusalFINPreservesReplyWhileOwnedCloseDrains(t *testing.T) {
	connection, client, _ := refusedTCP(t)
	done := make(chan error, 1)
	go func() { done <- connection.Close() }()
	if _, err := io.WriteString(client, strings.Repeat("x", 100000)); err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(client)
	if err != nil || string(raw) != incompleteHeadResponse {
		t.Fatal("refusal was reset or truncated", string(raw), err)
	}
	select {
	case err := <-done:
		t.Fatal("owned close stopped draining before peer EOF", err)
	default:
	}
	if err := client.CloseWrite(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("owned close did not finish after peer EOF")
	}
}

func TestServiceLifetimeInterruptsQuietAndSendingRefusedPeers(t *testing.T) {
	for _, sending := range []bool{false, true} {
		t.Run(map[bool]string{false: "quiet", true: "sending"}[sending], func(t *testing.T) {
			connection, client, cancel := refusedTCP(t)
			done := make(chan error, 2)
			go func() { done <- connection.Close() }()
			go func() { done <- connection.Close() }()
			raw, err := io.ReadAll(client)
			if err != nil || string(raw) != incompleteHeadResponse {
				t.Fatal(string(raw), err)
			}
			if sending {
				if _, err := io.WriteString(client, strings.Repeat("x", 100000)); err != nil {
					t.Fatal(err)
				}
			}
			cancel()
			for index := 0; index < 2; index++ {
				select {
				case err := <-done:
					if err != nil {
						t.Fatal(err)
					}
				case <-time.After(5 * time.Second):
					t.Fatal("service lifetime did not interrupt the refused reader")
				}
			}
		})
	}
}
