// The disposable decision adapter has no listener, storage, or process effects.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/caltseng/phalanx/server/internal/application"
)

func run(input io.Reader, output io.Writer) error {
	decoder, encoder := json.NewDecoder(input), json.NewEncoder(output)
	for {
		var request struct {
			ID      string                      `json:"id"`
			State   application.State           `json:"state"`
			Event   application.Event           `json:"event"`
			Options application.DecisionOptions `json:"options"`
		}
		if err := decoder.Decode(&request); err == io.EOF {
			return nil
		} else if err != nil {
			return err
		}
		next, err := application.Decide(request.State, request.Event, request.Options)
		var result any
		if err != nil {
			result = map[string]any{"id": request.ID, "error": err.Error()}
		} else {
			result = map[string]any{"id": request.ID, "state": next}
		}
		if err := encoder.Encode(result); err != nil {
			return err
		}
	}
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
