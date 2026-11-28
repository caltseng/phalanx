//go:build unix

package httpapi

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestStaticRefusesFIFOWithoutOpeningIt(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "pipe")
	if err := unix.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	handler := Handler{AssetsDirectory: directory}
	for _, method := range []string{"GET", "HEAD"} {
		t.Run(method, func(t *testing.T) {
			response := httptest.NewRecorder()
			done := make(chan struct{})
			go func() {
				defer close(done)
				handler.ServeHTTP(response, httptest.NewRequest(method, "/operator/assets/pipe", nil))
			}()
			select {
			case <-done:
				if response.Code != 404 {
					t.Fatalf("FIFO returned %d, want 404", response.Code)
				}
			case <-time.After(5 * time.Second):
				// Release a regressed blocking reader before temporary-file cleanup.
				file, err := os.OpenFile(path, os.O_RDWR|unix.O_NONBLOCK, 0)
				if err != nil {
					t.Fatal(err)
				}
				defer file.Close()
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Fatal("FIFO request did not stop after releasing the reader")
				}
				t.Fatal("FIFO request blocked instead of refusing the non-regular file")
			}
		})
	}
}
