package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
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
	return connection, client, cancel
}

func sendRefusal(connection *idleConnection) {
	connection.headers.mu.Lock()
	connection.refuseHeadLocked()
	connection.headers.mu.Unlock()
}

func TestRefusalFINPreservesReplyAndDisposesBeforePeerEOF(t *testing.T) {
	connection, client, _ := refusedTCP(t)
	if _, err := io.WriteString(client, strings.Repeat("x", 17000)); err != nil {
		t.Fatal(err)
	}
	sendRefusal(connection)
	done := make(chan error, 1)
	go func() { done <- connection.Close() }()
	raw, err := io.ReadAll(client)
	if err != nil || string(raw) != incompleteHeadResponse {
		t.Fatal("refusal was reset or truncated", string(raw), err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("owned close retained resources while peer kept its write side open")
	}
}

func TestRefusedCloseDisposesQuietAndSendingPeersWithoutLifetimeCancellation(t *testing.T) {
	for _, sending := range []bool{false, true} {
		t.Run(map[bool]string{false: "quiet", true: "sending"}[sending], func(t *testing.T) {
			connection, client, _ := refusedTCP(t)
			sendRefusal(connection)
			done := make(chan error, 2)
			raw, err := io.ReadAll(client)
			if err != nil || string(raw) != incompleteHeadResponse {
				t.Fatal(string(raw), err)
			}

			writerDone := make(chan struct{})
			go func() {
				defer close(writerDone)
				if sending {
					for index := 0; index < 1000; index++ {
						if _, err := io.WriteString(client, strings.Repeat("x", 1024)); err != nil {
							return
						}
					}
				}
			}()
			go func() { done <- connection.Close() }()
			go func() { done <- connection.Close() }()
			for index := 0; index < 2; index++ {
				select {
				case err := <-done:
					if err != nil {
						t.Fatal(err)
					}
				case <-time.After(5 * time.Second):
					t.Fatal("refused reader retained resources before service cancellation")
				}
			}
			<-writerDone
		})
	}
}

func TestRefusedCloseCompletesWithServiceCancellation(t *testing.T) {
	connection, _, cancel := refusedTCP(t)
	sendRefusal(connection)
	cancel()
	if err := connection.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRefusedHTTPConnectionsDoNotAccumulateBeforePeerEOF(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	closed := make(chan net.Conn, 16)
	server := &http.Server{
		Handler: http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
			t.Error("refused bytes reached request dispatch")
		}),
		ConnState: func(connection net.Conn, state http.ConnState) {
			idleState(connection, state)
			if state == http.StateClosed {
				closed <- connection.(*idleConnection).Conn
			}
		},
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(idleListener{Listener: listener, lifetime: ctx}) }()
	t.Cleanup(func() {
		cancel()
		server.Close()
		if err := <-done; !errors.Is(err, http.ErrServerClosed) {
			t.Error(err)
		}
	})
	for index := 0; index < 16; index++ {
		client, err := net.Dial("tcp", listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { client.Close() })
		if err := client.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
			t.Fatal(err)
		}
		request := "GET /health HTTP/1.1\r\nHost: localhost\r\nX-Refused: " + strings.Repeat("x", 17000)
		if _, err := io.WriteString(client, request); err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(client)
		if err != nil || string(raw) != incompleteHeadResponse {
			t.Fatal(string(raw), err)
		}
		select {
		case native := <-closed:
			if _, err := native.Read(make([]byte, 1)); !errors.Is(err, net.ErrClosed) {
				t.Fatal("native socket retained after HTTP close", err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("HTTP connection tracking retained an EOF-open refused peer")
		}
	}
	if ctx.Err() != nil {
		t.Fatal("test disposed peers only by service cancellation")
	}
}
