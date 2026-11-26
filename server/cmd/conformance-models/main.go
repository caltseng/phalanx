package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func main() {
	allowNaive := flag.Bool("allow-naive", false, "compare datetime rather than AwareDatetime")
	flag.Parse()
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 2*1024*1024)
	encoder := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		value, _, err := contract.Decode(scanner.Bytes())
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		var result any
		var issues []contract.Issue
		if *allowNaive {
			result, issues = contract.ValidateDateTime(value, []any{"body", "observed_at"})
		} else {
			result, issues = contract.ValidateAwareTime(value, []any{"body", "observed_at"})
		}
		var response any = map[string]any{"value": result}
		if len(issues) != 0 {
			response = map[string]any{"errors": issues}
		}
		if err := encoder.Encode(response); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
