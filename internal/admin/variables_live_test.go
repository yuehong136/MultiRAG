package admin

import (
	"encoding/json"
	"multirag/internal/cli"
	"multirag/internal/entity"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func adminCLI(t *testing.T, f *adminFixture) *cli.CLI {
	t.Helper()
	address, err := url.Parse(f.url)
	if err != nil {
		t.Fatal(err)
	}
	h := cli.NewHTTPClient()
	h.Host = address.Hostname()
	h.Port, _ = strconv.Atoi(address.Port())
	token := f.tokens["admin"]
	h.LoginToken = &token
	return &cli.CLI{Config: &cli.CommandLineConfig{CLIMode: cli.AdminMode}, AdminServerClient: h}
}
func TestAdminVariablesCLIHTTPAndSQL(t *testing.T) {
	f := newAdminFixture(t)
	for _, setting := range []entity.SystemSettings{{Name: "mail.server", DataType: "string", Value: "before", Source: "admin"}, {Name: "mail.port", DataType: "integer", Value: "15", Source: "admin"}, {Name: "mail.use_tls", DataType: "boolean", Value: "true", Source: "admin"}, {Name: "literal_%a", DataType: "string", Value: "literal", Source: "admin"}, {Name: "literalXXa", DataType: "string", Value: "wildcard", Source: "admin"}, {Name: "bang!_%a", DataType: "string", Value: "literal-bang", Source: "admin"}} {
		if err := f.DB.Create(&setting).Error; err != nil {
			t.Fatal(err)
		}
	}
	client := adminCLI(t, f)
	execute := func(text string) (cli.ResponseIf, error) {
		cmd, err := cli.NewParser(text).Parse(cli.AdminMode)
		if err != nil {
			return nil, err
		}
		return client.ExecuteCommand(cmd)
	}
	snapshot := func() string {
		t.Helper()
		rows, err := f.Read.Query("SELECT name,value,data_type,source,create_time,update_time FROM system_settings ORDER BY name")
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var result [][]any
		for rows.Next() {
			var name, value, kind, source string
			var create, update int64
			if err := rows.Scan(&name, &value, &kind, &source, &create, &update); err != nil {
				t.Fatal(err)
			}
			result = append(result, []any{name, value, kind, source, create, update})
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(result)
		return string(raw)
	}
	result, err := execute("LIST VARS;")
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, row := range result.(*cli.CommonResponse).Data {
		names = append(names, row["name"].(string))
		if row["setting_type"] != "config" || row["source"] != nil {
			t.Fatal("variable CLI mapping lost", row)
		}
	}
	ordered, err := f.Read.Query("SELECT name FROM system_settings ORDER BY name ASC")
	if err != nil {
		t.Fatal(err)
	}
	var wanted []string
	for ordered.Next() {
		var name string
		if err := ordered.Scan(&name); err != nil {
			t.Fatal(err)
		}
		wanted = append(wanted, name)
	}
	ordered.Close()
	if !reflect.DeepEqual(names, wanted) {
		t.Fatal("variables changed target SQL order", names, wanted)
	}
	for _, tc := range []struct{ text, name, value string }{{"SET VAR mail.port 0;", "mail.port", "0"}, {"SET VAR mail.use_tls false;", "mail.use_tls", "false"}, {"SET VAR mail.server '';", "mail.server", ""}, {`SET VAR sandbox.owned '{"enabled":false}';`, "sandbox.owned", `{"enabled":false}`}, {"SET VAR feature.enabled false;", "feature.enabled", "false"}, {"SET VAR mail exact;", "mail", "exact"}} {
		if _, err := execute(tc.text); err != nil {
			t.Fatal(tc.text, err)
		}
		var value string
		if err := f.Read.QueryRow("SELECT value FROM system_settings WHERE name=?", tc.name).Scan(&value); err != nil || value != tc.value {
			t.Fatal("committed variable mismatch", tc.name, err)
		}
	}
	for _, name := range []string{"mail", "literal_%", "bang!_%"} {
		cmd := &cli.Command{Type: "show_variable", Params: map[string]interface{}{"var_name": name}}
		result, err := client.ExecuteCommand(cmd)
		if err != nil || len(result.(*cli.CommonResponse).Data) != 1 {
			t.Fatal("exact/literal prefix mismatch", name, err)
		}
	}
	before := snapshot()
	for _, text := range []string{"SET VAR mail.port invalid;", "SET VAR mail.use_tls yes;", "SET VAR sandbox.new invalid;", "SET VAR new.enabled invalid;", "SHOW VAR missing;"} {
		if _, err := execute(text); err == nil {
			t.Fatal("invalid value accepted", text)
		}
		if snapshot() != before {
			t.Fatal("invalid CLI changed SQL")
		}
	}
	for _, actor := range []string{"", "normal", "inactive", "nil-admin"} {
		status, result := f.request(t, actor, "PUT", "/variables", `{"var_name":"mail.port","var_value":"42"}`)
		if status != 401 && status != 403 {
			t.Fatal("permission accepted", status, result)
		}
		if snapshot() != before {
			t.Fatal("denied mutation changed SQL")
		}
	}
	for _, body := range []string{`{}`, `{"var_name":"mail.port"}`, `{"var_name":"mail.port","var_value":0}`, `{"var_name":"mail.use_tls","var_value":false}`, `{"var_name":"mail.server","var_value":null}`} {
		_, result := f.request(t, "admin", "PUT", "/variables", body)
		if result["code"] == float64(0) || snapshot() != before {
			t.Fatal("invalid variable body accepted", body, result)
		}
	}
	req, _ := http.NewRequest("GET", f.url+"/api/v1/admin/variables", strings.NewReader(`{"var_name":"mail"}`))
	req.TransferEncoding = []string{"chunked"}
	req.ContentLength = -1
	req.Header.Set("Authorization", f.tokens["admin"])
	response, err := f.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 400 || snapshot() != before {
		t.Fatal("unknown length body accepted")
	}
}
