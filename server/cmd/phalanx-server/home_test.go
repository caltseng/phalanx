package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestConfiguredHomeCanonicalizesOnlyAbsoluteExpandedPaths(t *testing.T) {
	root := t.TempDir()
	var err error
	root, err = resolveExistingHome(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "child"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "file"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	lookup := func(key string) (string, bool) {
		if key == "HOME" || key == "USERPROFILE" {
			return root, true
		}
		return "", false
	}
	for _, test := range []struct{ path, want string }{
		{"", ""}, {"relative", ""}, {"child/..", ""},
		{root, root},
		{root + "/child/..", root},
		{root + "/missing/../next", filepath.Join(root, "next")},
		{root + "/file/child", filepath.Join(root, "file", "child")},
		{"~/child/..", root},
	} {
		t.Run(test.path, func(t *testing.T) {
			home, err := configuredHome(test.path, lookup)
			if err != nil {
				t.Fatal(err)
			}
			if test.want == "" {
				if home != nil {
					t.Fatalf("relative/unset home exposed: %q", *home)
				}
			} else if home == nil || *home != test.want {
				t.Fatalf("home %v != %q", home, test.want)
			}
		})
	}
	if runtime.GOOS != "windows" {
		link := filepath.Join(root, "link")
		if err := os.Symlink(filepath.Join(root, "child"), link); err != nil {
			t.Fatal(err)
		}
		for _, path := range []string{link + "/..", link + "/missing/../.."} {
			home, err := configuredHome(path, lookup)
			if err != nil || home == nil || *home != root {
				t.Fatalf("symlink-parent resolution: %v, %v", home, err)
			}
		}
		home, err := configuredHome(root+"/missing/../link", lookup)
		if err != nil || home == nil || *home != filepath.Join(root, "child") {
			t.Fatalf("resolution did not resume after missing parent: %v, %v", home, err)
		}
		dangling := filepath.Join(root, "dangling")
		if err := os.Symlink(filepath.Join(root, "child", "missing"), dangling); err != nil {
			t.Fatal(err)
		}
		home, err = configuredHome(dangling+"/..", lookup)
		if err != nil || home == nil || *home != filepath.Join(root, "child") {
			t.Fatalf("dangling link parent lost its target: %v, %v", home, err)
		}
	}
}
