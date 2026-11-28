package contract

import (
	"go/ast"
	goparser "go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strconv"
	"testing"
)

func TestRegexCompilationIsPackageScoped(t *testing.T) {
	fset := token.NewFileSet()
	err := filepath.WalkDir("..", func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || filepath.Ext(path) != ".go" {
			return err
		}
		file, err := goparser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		for _, imp := range file.Imports {
			source, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				return err
			}
			if source != "regexp" {
				continue
			}
			name := "regexp"
			if imp.Name != nil {
				name = imp.Name.Name
			}
			ast.Inspect(file, func(node ast.Node) bool {
				var body *ast.BlockStmt
				switch node := node.(type) {
				case *ast.FuncDecl:
					body = node.Body
				case *ast.FuncLit:
					body = node.Body
				}
				if body == nil {
					return true
				}
				ast.Inspect(body, func(node ast.Node) bool {
					call, ok := node.(*ast.CallExpr)
					if !ok {
						return true
					}
					function := ""
					switch target := call.Fun.(type) {
					case *ast.SelectorExpr:
						if pkg, ok := target.X.(*ast.Ident); ok && pkg.Name == name {
							function = target.Sel.Name
						}
					case *ast.Ident:
						if name == "." {
							function = target.Name
						}
					}
					if function == "Compile" || function == "MustCompile" {
						t.Errorf("%s: compile regexes at package scope, not per call", fset.Position(call.Pos()))
					}
					return true
				})
				return false
			})
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestCanonicalUnicodeDefaultsPresenceAndRetiredField(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":" \u96ea \ud83d\ude80 \u001c "}`))
	if err != nil {
		t.Fatal(err)
	}
	request, issues := ValidateIntake(value, keys)
	if len(issues) != 0 || request.Goal != "雪 🚀 \x1c" {
		t.Fatal(request, issues)
	}
	material, err := request.Material()
	if err != nil {
		t.Fatal(err)
	}
	material["runner_affinity"] = nil
	text, err := Canonical(material)
	expected := `{"acceptance_criteria":[],"context_links":[],"dependencies":[],"goal":"\u96ea \ud83d\ude80 \u001c","label":null,"priority":"normal","publication_mode":"ready","repository_id":"repo","runner_affinity":null,"target":"checked_in","target_branch":null}`
	if err != nil || text != expected {
		t.Fatal(text, err)
	}
	if _, issues := ValidateIntake(material, ObjectKeys(material)); len(issues) != 1 || issues[0].Loc[1] != "runner_affinity" {
		t.Fatal("internal null leaked into public contract", issues)
	}
}

func TestNoNumericCoercionOrPrecisionLoss(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":9007199254740993}`))
	if err != nil {
		t.Fatal(err)
	}
	_, issues := ValidateIntake(value, keys)
	if len(issues) != 1 || issues[0].Loc[1] != "goal" {
		t.Fatal(issues)
	}
}

func TestDuplicateJSONLastWinsAndExtras(t *testing.T) {
	value, keys, err := Decode([]byte(`{"repository_id":"repo","goal":"old","z":1,"a":2,"goal":"new"}`))
	if err != nil {
		t.Fatal(err)
	}
	request, issues := ValidateIntake(value, keys)
	if request.Goal != "new" || len(issues) != 2 || issues[0].Loc[1] != "a" || issues[1].Loc[1] != "z" {
		t.Fatal(request, issues)
	}
}
