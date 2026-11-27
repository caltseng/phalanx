package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigurationPreservesExplicitSettingsAndRefusesUnsafeStartup(t *testing.T) {
	assets := t.TempDir()
	for _, name := range []string{"operator-board.html", "operator-board.js", "operator-board.css",
		"operator-helpers.js", "operator-http.js", "operator-markdown.js", "operator-shell.js", "phalanx-brand.svg"} {
		if err := os.WriteFile(filepath.Join(assets, name), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	env := map[string]string{"PHALANX_DB": filepath.Join(t.TempDir(), "current.sqlite3"),
		"PHALANX_OWNER_TOKEN": strings.Repeat("o", 32), "PHALANX_RUNNER_TOKEN": strings.Repeat("r", 32),
		"PHALANX_HOME": t.TempDir(), "PHALANX_LOCAL_BOARD": "1"}
	canonical, err := resolveExistingHome(env["PHALANX_HOME"])
	if err != nil {
		t.Fatal(err)
	}
	config, err := configure("::1", 8765, assets, func(key string) (string, bool) { value, ok := env[key]; return value, ok })
	if err != nil || config.database != env["PHALANX_DB"] || config.home == nil ||
		*config.home != canonical || !config.localBoard || config.port != 8765 {
		t.Fatalf("Configuration changed explicit settings: %+v, %v", config, err)
	}
	for _, test := range []struct{ key, value string }{
		{"PHALANX_DB", "relative.sqlite3"}, {"PHALANX_OWNER_TOKEN", "secret"},
		{"PHALANX_RUNNER_TOKEN", env["PHALANX_OWNER_TOKEN"]}, {"PHALANX_LOCAL_BOARD", "true"},
	} {
		t.Run(test.key, func(t *testing.T) {
			values := make(map[string]string)
			for key, value := range env {
				values[key] = value
			}
			values[test.key] = test.value
			if _, err := configure("127.0.0.1", 8765, assets, func(key string) (string, bool) { value, ok := values[key]; return value, ok }); err == nil {
				t.Fatal("Unsafe settings accepted")
			}
		})
	}
	if _, err := configure("0.0.0.0", 8765, assets, func(key string) (string, bool) { value, ok := env[key]; return value, ok }); err == nil {
		t.Fatal("Changed service exposure accepted")
	}
	if _, err := configure("127.0.0.1", 8765, t.TempDir(), func(key string) (string, bool) { value, ok := env[key]; return value, ok }); err == nil {
		t.Fatal("Missing packaged Scribe accepted")
	}
}
