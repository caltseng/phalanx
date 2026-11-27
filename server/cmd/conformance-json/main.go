// Pure disposable canonical encoding proof; no listener, files, or credentials.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func run() error {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 256000)
	encoder := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		value, _, err := contract.Decode(scanner.Bytes())
		if err != nil {
			return err
		}
		canonical, err := contract.Canonical(value)
		var result any
		if err != nil {
			result = map[string]any{"error": err.Error()}
		} else {
			result = map[string]any{"canonical": canonical, "digest": contract.Digest(canonical)}
		}
		if err := encoder.Encode(result); err != nil {
			return err
		}
	}
	return scanner.Err()
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
