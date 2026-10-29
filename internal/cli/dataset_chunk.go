package cli

import (
	"encoding/json"
	"fmt"
	"net/url"
	"sort"
	"strings"
	"time"
)

// finishDatasetCommand enforces one terminated statement.
func (p *Parser) finishDatasetCommand() error {
	if p.curToken.Type != TokenSemicolon {
		return fmt.Errorf("expected semicolon")
	}
	p.nextToken()
	if p.curToken.Type != TokenEOF {
		return fmt.Errorf("unexpected trailing input: %s", p.curToken.Value)
	}
	return nil
}

func (p *Parser) parseGetCommand() (*Command, error) {
	p.nextToken()
	if p.curToken.Type == TokenMetadata {
		return p.parseGetMetadata()
	}
	if p.curToken.Type != TokenChunk {
		return nil, fmt.Errorf("expected CHUNK after GET")
	}
	cmd, err := p.parseChunkIdentity("get_chunk")
	if err != nil {
		return nil, err
	}
	if err := p.finishDatasetCommand(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseChunkIdentity consumes CHUNK 'id' OF DOCUMENT 'id' IN DATASET 'reference'.
func (p *Parser) parseChunkIdentity(kind string) (*Command, error) {
	p.nextToken()
	cmd := NewCommand(kind)
	for i, field := range []string{"chunk_id", "doc_id", "dataset_name"} {
		value, err := p.parseQuotedString()
		if err != nil || strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("non-empty %s required", field)
		}
		cmd.Params[field] = value
		p.nextToken()
		expected := [][]int{{TokenOf, TokenDocument}, {TokenIn, TokenDataset}, {}}[i]
		for _, token := range expected {
			if p.curToken.Type != token {
				return nil, fmt.Errorf("unexpected chunk identity token: %s", p.curToken.Value)
			}
			p.nextToken()
		}
	}
	return cmd, nil
}

func (p *Parser) parseGetMetadata() (*Command, error) {
	p.nextToken()
	for _, token := range []int{TokenOf, TokenDataset} {
		if p.curToken.Type != token {
			return nil, fmt.Errorf("expected OF DATASET after METADATA")
		}
		p.nextToken()
	}
	names := []string{}
	seen := map[string]bool{}
	for p.curToken.Type == TokenQuotedString {
		name, err := p.parseQuotedString()
		if err != nil || strings.TrimSpace(name) == "" || seen[name] {
			return nil, fmt.Errorf("distinct non-empty dataset names required")
		}
		seen[name] = true
		names = append(names, name)
		p.nextToken()
	}
	if len(names) == 0 {
		return nil, fmt.Errorf("dataset names required")
	}
	if err := p.finishDatasetCommand(); err != nil {
		return nil, err
	}
	cmd := NewCommand("get_metadata")
	cmd.Params["dataset_names"] = names
	return cmd, nil
}

func (c *MultiRAGClient) GetChunk(cmd *Command) (ResponseIf, error) {
	started := time.Now()
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	values := make(map[string]string)
	for _, key := range []string{"dataset_name", "doc_id", "chunk_id"} {
		value, _ := cmd.Params[key].(string)
		if strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("%s required", key)
		}
		values[key] = value
	}
	datasetID, err := c.documentDatasetID(values["dataset_name"])
	if err != nil {
		return nil, err
	}
	path := "/datasets/" + url.PathEscape(datasetID) + "/documents/" + url.PathEscape(values["doc_id"]) + "/chunks/" + url.PathEscape(values["chunk_id"])
	response, err := c.HTTPClient.RequestBounded("GET", path, "api", nil, nil, 20*1024*1024)
	if err != nil {
		return nil, err
	}
	data, err := decodeDatasetObject("get chunk", response)
	if err != nil {
		return nil, err
	}
	if data["id"] != values["chunk_id"] || data["doc_id"] != values["doc_id"] {
		return nil, fmt.Errorf("chunk response identity mismatch")
	}
	return &CommonDataResponse{Data: data, Duration: time.Since(started).Seconds()}, nil
}

func (c *MultiRAGClient) GetMetadata(cmd *Command) (ResponseIf, error) {
	started := time.Now()
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	names, _ := cmd.Params["dataset_names"].([]string)
	if len(names) == 0 {
		return nil, fmt.Errorf("dataset names required")
	}
	ids := []string{}
	seen := map[string]bool{}
	for _, name := range names {
		if strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("dataset names required")
		}
		id, err := c.documentDatasetID(name)
		if err != nil {
			return nil, err
		}
		if seen[id] {
			return nil, fmt.Errorf("duplicate dataset scope")
		}
		seen[id] = true
		ids = append(ids, id)
	}
	query := url.Values{"dataset_ids": {strings.Join(ids, ",")}}
	response, err := c.HTTPClient.RequestBounded("GET", "/datasets/metadata/flattened?"+query.Encode(), "api", nil, nil, 20*1024*1024)
	if err != nil {
		return nil, err
	}
	data, err := decodeDatasetObject("get metadata", response)
	if err != nil {
		return nil, err
	}
	if _, err := metadataTable(data); err != nil {
		return nil, err
	}
	return &MetadataResponse{Data: data, Duration: time.Since(started).Seconds()}, nil
}

func decodeDatasetObject(operation string, response *Response) (map[string]any, error) {
	if err := checkAuthResponse(operation, response); err != nil {
		return nil, err
	}
	var result struct {
		Data *map[string]any `json:"data"`
	}
	if err := json.Unmarshal(response.Body, &result); err != nil || result.Data == nil {
		return nil, fmt.Errorf("%s returned an invalid object", operation)
	}
	return *result.Data, nil
}

type MetadataResponse struct {
	Data         map[string]any `json:"data"`
	Duration     float64        `json:"-"`
	outputFormat OutputFormat
}

func (r *MetadataResponse) Type() string                        { return "metadata" }
func (r *MetadataResponse) TimeCost() float64                   { return r.Duration }
func (r *MetadataResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }
func (r *MetadataResponse) PrintOut() {
	if r.outputFormat == OutputFormatJSON {
		raw, _ := json.MarshalIndent(r.Data, "", "  ")
		fmt.Println(string(raw))
		return
	}
	rows, err := metadataTable(r.Data)
	if err != nil {
		fmt.Println(err)
		return
	}
	PrintTableSimpleByFormat(rows, r.outputFormat)
}

func metadataTable(data map[string]any) ([]map[string]any, error) {
	rows := []map[string]any{}
	for field, value := range data {
		values, ok := value.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("invalid flattened metadata field")
		}
		for label, value := range values {
			list, ok := value.([]any)
			if !ok {
				return nil, fmt.Errorf("invalid metadata document IDs")
			}
			ids := []string{}
			seen := map[string]bool{}
			for _, item := range list {
				id, ok := item.(string)
				if !ok || strings.TrimSpace(id) == "" || seen[id] {
					return nil, fmt.Errorf("invalid metadata document ID")
				}
				seen[id] = true
				ids = append(ids, id)
			}
			sort.Strings(ids)
			rows = append(rows, map[string]any{"field": field, "value": label, "document_ids": strings.Join(ids, ", ")})
		}
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i]["field"] == rows[j]["field"] {
			return rows[i]["value"].(string) < rows[j]["value"].(string)
		}
		return rows[i]["field"].(string) < rows[j]["field"].(string)
	})
	return rows, nil
}

func (c *MultiRAGClient) removeDatasetTags(cmd *Command) (ResponseIf, error) {
	started := time.Now()
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	name, _ := cmd.Params["dataset_name"].(string)
	tags, _ := cmd.Params["tags"].([]string)
	if strings.TrimSpace(name) == "" || len(tags) == 0 {
		return nil, fmt.Errorf("dataset and non-empty tags required")
	}
	seen := map[string]bool{}
	for _, tag := range tags {
		if strings.TrimSpace(tag) == "" || seen[tag] {
			return nil, fmt.Errorf("distinct non-empty tags required")
		}
		seen[tag] = true
	}
	dataset, err := c.documentDatasetID(name)
	if err != nil {
		return nil, err
	}
	response, err := c.HTTPClient.RequestBounded("DELETE", "/datasets/"+url.PathEscape(dataset)+"/tags", "api", nil, map[string]any{"tags": tags}, 1024*1024)
	if err != nil {
		return nil, err
	}
	if err = checkAuthResponse("remove tags", response); err != nil {
		return nil, err
	}
	var envelope struct {
		Data *bool `json:"data"`
	}
	if err = json.Unmarshal(response.Body, &envelope); err != nil || envelope.Data == nil || !*envelope.Data {
		return nil, fmt.Errorf("invalid tag deletion result")
	}
	return &SimpleResponse{Message: "Tags removed", Duration: time.Since(started).Seconds()}, nil
}
