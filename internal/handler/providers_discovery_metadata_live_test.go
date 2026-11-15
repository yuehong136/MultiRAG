package handler_test

import (
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestSupportedDiscoveryMetadataHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	var mode atomic.Int32
	key := "private-discovery-key"
	supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "GET" || r.ContentLength > 0 || r.Header.Get("Authorization") != "Bearer "+key || !strings.HasSuffix(r.URL.Path, "models") {
			t.Error("native discovery credential/method/path changed")
		}
		switch mode.Load() {
		case 1:
			fmt.Fprint(w, `{"data":null}`)
		case 2:
			fmt.Fprint(w, `{"error":{"message":"private-discovery-body"}}`)
		case 3:
			w.WriteHeader(503)
			fmt.Fprint(w, "private-discovery-body")
		default:
			fmt.Fprint(w, `{"data":[{"id":"remote-unique"}]}`)
		}
	}))
	f.enroll(t, "OpenAI", "one", key, supplier.URL, "declared-custom", "chat")
	before := f.snapshot(t)
	result := f.requireSuccess(t, "owner", "GET", "/api/v1/providers/OpenAI/instances/one/models?supported=true", nil)
	encoded, _ := json.Marshal(result["data"])
	if !strings.Contains(string(encoded), "remote-unique") {
		t.Fatal("actual remote discovery identity missing")
	}
	f.requireSuccess(t, "owner", "GET", "/api/v1/providers/OpenAI/instances/one/connection", nil)
	if calls.Load() != 2 || f.snapshot(t) != before {
		t.Fatal("discovery promoted or wrote SQL")
	}
	for _, modeValue := range []int32{1, 2, 3} {
		mode.Store(modeValue)
		_, failed, raw := f.request(t, "owner", "GET", "/api/v1/providers/OpenAI/instances/one/models?supported=true", nil)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-discovery-body") {
			t.Fatal("bad inventory accepted or leaked")
		}
	}
	count := calls.Load()
	for _, who := range []string{"", "foreign", "inactive"} {
		_, failed, raw := f.request(t, who, "GET", "/api/v1/providers/OpenAI/instances/one/models?supported=true", nil)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, key) {
			t.Fatal("foreign discovery accepted or leaked")
		}
	}
	if calls.Load() != count || f.snapshot(t) != before {
		t.Fatal("denied discovery side effects")
	}
	own := f.requireSuccess(t, "owner", "GET", "/api/v1/providers/OpenAI/instances/one", nil)["data"].(map[string]any)
	if own["api_key"] != key || own["instance_name"] != "one" || own["base_url"] != supplier.URL || own["region"] != "default" {
		t.Fatal("canonical instance fields lost")
	}
	for _, who := range []string{"foreign", "inactive"} {
		_, failed, raw := f.request(t, who, "GET", "/api/v1/providers/OpenAI/instances/one", nil)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, key) {
			t.Fatal("instance secret crossed tenant")
		}
	}
	// Catalog metadata is global, non-secret, and comes directly from the frozen model config.
	catalog := dao.GetModelProviderManager().FindProvider("OpenAI")
	name := catalog.Models[0].Name
	for _, who := range []string{"owner", "foreign"} {
		r := f.requireSuccess(t, who, "GET", "/api/v1/providers/OpenAI/models/"+name, nil)
		b, _ := json.Marshal(r)
		if strings.Contains(string(b), key) || strings.Contains(string(b), supplier.URL) {
			t.Fatal("static model metadata contains tenant credentials")
		}
	}
	f.requireSuccess(t, "owner", "GET", "/api/v1/providers/?available=true", nil)
	if f.snapshot(t) != before || calls.Load() != count {
		t.Fatal("metadata read mutated SQL or called supplier")
	}
}

func TestCanonicalSupplierDiscoveryHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, provider := range []string{"DeepSeek", "HuaweiCloud", "ModelScope", "OrcaRouter", "Qiniu", "CoHere", "FishAudio", "StepFun", "Upstage", "Baichuan", "LocalAI", "SiliconFlow", "Gitee", "Aliyun", "VolcEngine", "NVIDIA", "HuggingFace", "GPUStack", "Azure-OpenAI", "lmstudio", "Baidu", "n1n", "LongCat", "xAI", "TokenHub"} {
		t.Run(provider, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) {
					var calls atomic.Int64
					var mode atomic.Int32
					key := f.Prefix + provider + instance
					if (provider == "ModelScope" || provider == "LocalAI") && instance == "one" {
						key = ""
					}
					name := "remote-" + provider + "-" + instance
					config := dao.GetModelProviderManager().FindProvider(provider)
					if config == nil {
						t.Fatal("provider absent", provider)
					}
					supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls.Add(1)
						auth := "Bearer " + key
						if key == "" {
							auth = ""
						}
						if provider == "CoHere" {
							auth = "bearer " + key
						}
						if provider == "Gitee" && r.URL.Path != "/"+strings.Trim(config.URLSuffix.Status, "/") {
							auth = ""
						}
						if r.Method != "GET" || provider != "Azure-OpenAI" && r.Header.Get("Authorization") != auth {
							t.Error("native discovery auth/method changed")
						}
						if provider == "Azure-OpenAI" && (r.Header.Get("api-key") != key || r.URL.Query().Get("api-version") == "") {
							t.Error("native Azure discovery auth/version lost")
						}
						if provider == "DeepSeek" || provider == "Gitee" && r.URL.Path == "/"+strings.Trim(config.URLSuffix.Status, "/") {
							body, _ := io.ReadAll(r.Body)
							if string(body) != "{}" {
								t.Error("frozen DeepSeek GET body changed")
							}
						} else if r.ContentLength > 0 {
							t.Error("native discovery gained body")
						}
						if provider == "Gitee" && r.URL.Path == "/"+strings.Trim(config.URLSuffix.Status, "/") {
							fmt.Fprint(w, `{}`)
							return
						}
						if mode.Load() == 1 {
							fmt.Fprint(w, `{}`)
							return
						}
						if mode.Load() == 2 {
							w.WriteHeader(503)
							fmt.Fprint(w, "private-discovery-body")
							return
						}
						if provider == "FishAudio" && strings.HasSuffix(r.URL.Path, "wallet/self/api-credit") {
							fmt.Fprint(w, `{"credit":0}`)
							return
						}
						if provider == "VolcEngine" && r.URL.Path == "/"+config.URLSuffix.Files {
							fmt.Fprint(w, `{}`)
							return
						}
						if !strings.HasSuffix(r.URL.Path, config.URLSuffix.Models) {
							t.Error("native discovery suffix changed", r.URL.Path)
						}
						switch provider {
						case "CoHere":
							fmt.Fprintf(w, `{"models":[{"name":%q}]}`, name)
						case "FishAudio":
							fmt.Fprintf(w, `{"items":[{"_id":"voice-id","title":%q}]}`, name)
						default:
							fmt.Fprintf(w, `{"data":[{"id":%q}]}`, name)
						}
					}))
					id := f.enroll(t, provider, "catalog-"+instance, key, supplier.URL, "declared-custom", "chat")
					before := f.snapshot(t)
					catalogBefore, _ := json.Marshal(config.Models)
					path := "/api/v1/providers/" + provider + "/instances/catalog-" + instance
					unsupported := provider == "Baichuan"
					for _, route := range []string{path + "/models?supported=true", path + "/connection"} {
						unsupportedOperation := unsupported || provider == "Qiniu" && strings.HasSuffix(route, "/connection")
						count := calls.Load()
						_, result, raw := f.request(t, "owner", "GET", route, nil)
						if unsupportedOperation {
							if providerEnvelopeError(result) == nil || calls.Load() != count {
								t.Fatal("unsupported catalog contacted supplier or succeeded")
							}
						} else if providerEnvelopeError(result) != nil || strings.Contains(route, "models?") && !strings.Contains(raw, name) {
							t.Fatal("native discovery/connection result missing", result["message"])
						}
					}
					for _, syntax := range []string{fmt.Sprintf("LIST SUPPORTED MODELS FROM '%s' 'catalog-%s';", provider, instance), fmt.Sprintf("CHECK INSTANCE 'catalog-%s' FROM '%s';", instance, provider)} {
						cmd, err := cli.NewParser(syntax).Parse(cli.APIMode)
						if err != nil {
							t.Fatal(err)
						}
						count := calls.Load()
						result, err := f.cli(t, "owner").ExecuteCommand(cmd)
						if unsupported || provider == "Qiniu" && strings.HasPrefix(syntax, "CHECK") {
							if err == nil || calls.Load() != count {
								t.Fatal("unsupported CLI catalog fabricated success")
							}
						} else if err != nil {
							t.Fatal("actual discovery CLI", err)
						} else if strings.HasPrefix(syntax, "LIST") {
							raw, _ := json.Marshal(result)
							if !strings.Contains(string(raw), name) {
								t.Fatal("CLI inventory lost actual identity")
							}
						}
					}
					if provider == "FishAudio" {
						result := f.requireSuccess(t, "owner", "GET", path+"/balance", nil)
						if result["data"].(map[string]any)["credit"] != float64(0) {
							t.Fatal("legal zero credit lost")
						}
						cmd, err := cli.NewParser(fmt.Sprintf("SHOW BALANCE FROM '%s' 'catalog-%s';", provider, instance)).Parse(cli.APIMode)
						if err != nil {
							t.Fatal(err)
						}
						if _, err = f.cli(t, "owner").ExecuteCommand(cmd); err != nil {
							t.Fatal(err)
						}
						count := calls.Load()
						_, failed, _ := f.request(t, "owner", "POST", "/api/v1/chat/completions", map[string]any{"model_id": id, "messages": []any{map[string]any{"role": "user", "content": "q"}}})
						if providerEnvelopeError(failed) == nil || calls.Load() != count {
							t.Fatal("FishAudio unsupported chat called supplier")
						}
					}
					if f.snapshot(t) != before {
						t.Fatal("discovery promoted inventory or mutated SQL")
					}
					count := calls.Load()
					for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
						for _, suffix := range []string{"/models?supported=true", "/connection", "/balance"} {
							_, failed, raw := f.request(t, who, "GET", path+suffix, nil)
							if providerEnvelopeError(failed) == nil || key != "" && strings.Contains(raw, key) {
								t.Fatal("denied discovery or secret disclosure", who)
							}
						}
						cmd, err := cli.NewParser(fmt.Sprintf("LIST SUPPORTED MODELS FROM '%s' 'catalog-%s';", provider, instance)).Parse(cli.APIMode)
						if err != nil {
							t.Fatal(err)
						}
						if _, err = f.cli(t, who).ExecuteCommand(cmd); err == nil {
							t.Fatal("denied CLI inventory succeeded", who)
						}
					}
					if calls.Load() != count || f.snapshot(t) != before {
						t.Fatal("denied discovery changed SQL or contacted supplier")
					}
					if !unsupported {
						for _, value := range []int32{1, 2} {
							mode.Store(value)
							_, failed, raw := f.request(t, "owner", "GET", path+"/models?supported=true", nil)
							if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-discovery-body") {
								t.Fatal("missing or failed native inventory accepted/leaked")
							}
						}
					}
					catalogAfter, _ := json.Marshal(config.Models)
					if string(catalogBefore) != string(catalogAfter) {
						t.Fatal("native inventory mutated shared catalog metadata")
					}
					if f.snapshot(t) != before {
						t.Fatal("failed discovery wrote SQL")
					}
				})
			}
		})
	}
}

func TestRoot55ProviderBalancesHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, provider := range []string{"SiliconFlow", "DeepSeek", "Gitee"} {
		t.Run(provider, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) {
					var calls atomic.Int64
					var mode atomic.Int32
					config := dao.GetModelProviderManager().FindProvider(provider)
					if config == nil {
						t.Fatal("provider absent")
					}
					key := f.Prefix + provider + instance
					supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls.Add(1)
						if r.Method != "GET" || r.Header.Get("Authorization") != "Bearer "+key || !strings.HasSuffix(r.URL.Path, config.URLSuffix.Balance) {
							t.Error("native stored balance key/endpoint lost")
						}
						if mode.Load() == 2 {
							fmt.Fprint(w, `{}`)
							return
						}
						if mode.Load() == 3 {
							w.WriteHeader(503)
							fmt.Fprint(w, "private-balance-body")
							return
						}
						amount := "0"
						if mode.Load() == 1 {
							amount = "-0.25"
						}
						switch provider {
						case "DeepSeek":
							fmt.Fprintf(w, `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":%q,"granted_balance":"0","topped_up_balance":"0"}]}`, amount)
						case "SiliconFlow":
							fmt.Fprintf(w, `{"code":0,"status":true,"data":{"balance":%q,"totalBalance":%q}}`, amount, amount)
						case "Gitee":
							fmt.Fprintf(w, `{"balance":%s}`, amount)
						}
					}))
					f.enroll(t, provider, "balance-"+instance, key, supplier.URL, "declared-custom", "chat")
					var storedKey, extra string
					if err := f.Read.QueryRow("SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=? AND p.provider_name=? AND i.instance_name=?", "owner-tenant", provider, "balance-"+instance).Scan(&storedKey, &extra); err != nil {
						t.Fatal(err)
					}
					var saved map[string]string
					if json.Unmarshal([]byte(extra), &saved) != nil || storedKey != key || saved["base_url"] != supplier.URL || saved["region"] != "default" {
						t.Fatal("committed balance instance key/endpoint/region lost")
					}
					before := f.snapshot(t)
					path := "/api/v1/providers/" + provider + "/instances/balance-" + instance + "/balance"
					for _, m := range []int32{0, 1} {
						mode.Store(m)
						data := f.requireSuccess(t, "owner", "GET", path, nil)["data"].(map[string]any)
						want := float64(0)
						if m == 1 {
							want = -0.25
						}
						if data["balance"] != want || data["currency"] != "CNY" {
							t.Fatal("legal zero/debt balance lost")
						}
						cmd, err := cli.NewParser(fmt.Sprintf("SHOW BALANCE FROM '%s' 'balance-%s';", provider, instance)).Parse(cli.APIMode)
						if err != nil {
							t.Fatal(err)
						}
						result, err := f.cli(t, "owner").ExecuteCommand(cmd)
						if err != nil {
							t.Fatal(err)
						}
						raw, _ := json.Marshal(result)
						if !strings.Contains(string(raw), "CNY") {
							t.Fatal("CLI balance lost native result")
						}
					}
					count := calls.Load()
					for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
						_, failed, raw := f.request(t, who, "GET", path, nil)
						if providerEnvelopeError(failed) == nil || strings.Contains(raw, key) {
							t.Fatal("denied balance succeeded/leaked")
						}
					}
					if calls.Load() != count || f.snapshot(t) != before {
						t.Fatal("denied balance side effects")
					}
					for _, m := range []int32{2, 3} {
						mode.Store(m)
						_, failed, raw := f.request(t, "owner", "GET", path, nil)
						if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-balance-body") {
							t.Fatal("missing/failed native balance accepted/leaked")
						}
					}
					if f.snapshot(t) != before {
						t.Fatal("balance read modified SQL")
					}
				})
			}
		})
	}
}

func TestRoot55DiscoveryMetadataProjectionHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, provider := range []string{"SiliconFlow", "Gitee", "DeepSeek"} {
		t.Run(provider, func(t *testing.T) {
			config := dao.GetModelProviderManager().FindProvider(provider)
			if config == nil || len(config.Models) == 0 {
				t.Fatal("frozen metadata absent")
			}
			known := ""
			var expected *models.Model
			for _, definition := range config.Models {
				if metadata := models.GetProviderManager().GetModelByNameOrAlias(definition.Name); metadata != nil {
					known = definition.Name
					expected = metadata
					if metadata.MaxDimension != nil {
						break
					}
				}
			}
			if expected == nil {
				t.Fatal("known provider metadata missing")
			}
			var calls atomic.Int64
			var mode atomic.Int32
			key := f.Prefix + provider + "metadata"
			supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				auth := "Bearer " + key
				if provider == "Gitee" {
					auth = ""
				}
				if r.Method != "GET" || r.Header.Get("Authorization") != auth || !strings.HasSuffix(r.URL.Path, "models") {
					t.Error("native metadata discovery identity lost")
				}
				if mode.Load() == 1 {
					fmt.Fprint(w, `{"data":[]}`)
					return
				}
				if mode.Load() == 2 {
					fmt.Fprint(w, `{"data":[{"id":false}]}`)
					return
				}
				json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"id": known, "owned_by": "fixture"}, map[string]any{"id": "remote-unknown"}, map[string]any{"id": "remote-unknown"}}})
			}))
			f.enroll(t, provider, "metadata", key, supplier.URL, "declared-model", "chat")
			before := f.snapshot(t)
			catalogBefore, _ := json.Marshal(config.Models)
			path := "/api/v1/providers/" + provider + "/instances/metadata/models?supported=true"
			result := f.requireSuccess(t, "owner", "GET", path, nil)
			rows := result["data"].([]any)
			if len(rows) != 3 || rows[0].(map[string]any)["name"] != known+"@fixture" || rows[1].(map[string]any)["name"] != "remote-unknown" || rows[2].(map[string]any)["name"] != "remote-unknown" {
				t.Fatal("ordered native raw/duplicate IDs lost")
			}
			metadata := rows[0].(map[string]any)
			wantRaw, _ := json.Marshal(map[string]any{"max_dimension": expected.MaxDimension, "dimensions": expected.Dimensions, "max_tokens": expected.MaxTokens, "model_types": expected.ModelTypes, "thinking": expected.Thinking})
			var want map[string]any
			json.Unmarshal(wantRaw, &want)
			for field, value := range want {
				a, _ := json.Marshal(metadata[field])
				b, _ := json.Marshal(value)
				if string(a) != string(b) {
					t.Fatal("metadata field lost across actual HTTP", field)
				}
			}
			unknown := rows[1].(map[string]any)
			if unknown["model_types"] != nil || unknown["max_tokens"] != nil {
				t.Fatal("unknown inventory acquired invented catalog metadata")
			}
			cmd, err := cli.NewParser(fmt.Sprintf("LIST SUPPORTED MODELS FROM '%s' 'metadata';", provider)).Parse(cli.APIMode)
			if err != nil {
				t.Fatal(err)
			}
			response, err := f.cli(t, "owner").ExecuteCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			raw, _ := json.Marshal(response)
			var cliResult map[string]any
			json.Unmarshal(raw, &cliResult)
			if !reflect.DeepEqual(cliResult["data"], result["data"]) {
				t.Fatal("real CLI changed metadata/order/IDs")
			}
			for _, who := range []string{"", "foreign", "inactive"} {
				count := calls.Load()
				_, failed, _ := f.request(t, who, "GET", path, nil)
				if providerEnvelopeError(failed) == nil || calls.Load() != count {
					t.Fatal("foreign metadata reached supplier")
				}
			}
			mode.Store(1)
			empty := f.requireSuccess(t, "owner", "GET", path, nil)
			if empty["data"] != nil {
				t.Fatal("frozen ParseListModel empty result changed")
			}
			if _, err = f.cli(t, "owner").ExecuteCommand(cmd); err != nil {
				t.Fatal("valid empty native inventory rejected", err)
			}
			mode.Store(2)
			_, failed, _ := f.request(t, "owner", "GET", path, nil)
			if providerEnvelopeError(failed) == nil {
				t.Fatal("malformed inventory returned partial HTTP success")
			}
			if _, err = f.cli(t, "owner").ExecuteCommand(cmd); err == nil {
				t.Fatal("malformed inventory returned partial CLI success")
			}
			catalogAfter, _ := json.Marshal(config.Models)
			if string(catalogBefore) != string(catalogAfter) || f.snapshot(t) != before {
				t.Fatal("discovery changed shared catalog/SQL")
			}
		})
	}
}
