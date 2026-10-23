package cli

import "testing"

func TestParseAndProviderTaskSyntax(t *testing.T) {
	for input, kind := range map[string]string{
		"PARSE WITH 'MinerU2.5@one@Gitee' FILE 'document.pdf';":                 "parse_file_user_command",
		"PARSE WITH 'MinerU2.5@one@Gitee' URL 'https://files.invalid/doc.pdf';": "parse_file_user_command",
		"PARSE FILE 'document.pdf';":                                            "parse_file_user_command",
		"LIST 'one@Gitee' TASKS;":                                               "list_tasks_user_command",
		"SHOW 'one@Gitee' TASK 'job-1';":                                        "show_task_user_command",
	} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Type != kind {
			t.Fatalf("%s: %v %v", input, cmd, err)
		}
		if kind == "list_tasks_user_command" || kind == "show_task_user_command" {
			if cmd.Params["provider_name"] != "Gitee" || cmd.Params["instance_name"] != "one" {
				t.Fatal("task composite not resolved")
			}
		}
	}
	for _, input := range []string{
		"PARSE WITH 'bad' FILE 'a';", "PARSE WITH 'm@@Gitee' FILE 'a';", "PARSE FILE 'a' URL 'https://files.invalid/x';",
		"PARSE FILE 'a'; ignored", "PARSE FILE 'a'", "PARSE URL 'file:///secret';",
		"LIST 'one' TASKS;", "LIST '@Gitee' TASKS;", "LIST 'one@Gitee' TASKS; ignored",
		"SHOW 'one@Gitee' TASK '../secret';", "SHOW 'one@Gitee' TASK 'job-1'; ignored",
	} {
		if cmd, err := NewParser(input).Parse(false); err == nil {
			t.Fatal("accepted invalid syntax", input, cmd)
		}
	}
}

func TestParseTaskEnvelopeStrictness(t *testing.T) {
	for _, body := range []string{`{"data":{"task_id":"job","status":"pending"}}`, `{"code":0,"retcode":1,"data":{"task_id":"job","status":"pending"}}`, `{"code":0,"data":{}}`, `{"code":0,"data":{"task_id":"job","status":"success"}}`} {
		if _, err := parseTaskResponse(&Response{StatusCode: 200, Body: []byte(body)}, "task", false); err == nil {
			t.Fatal("invalid task response accepted", body)
		}
	}
	if _, err := parseTaskResponse(&Response{StatusCode: 200, Body: []byte(`{"retcode":0,"data":{"task_id":"job","status":"pending"}}`)}, "task", false); err != nil {
		t.Fatal(err)
	}
}
