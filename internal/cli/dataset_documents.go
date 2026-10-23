package cli

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/google/uuid"
)

type ListDocumentsResponse struct {
	Docs         []map[string]any `json:"docs"`
	Total        int64            `json:"total"`
	Page         int              `json:"page"`
	PageSize     int              `json:"page_size"`
	Duration     float64          `json:"-"`
	outputFormat OutputFormat
}

func (r *ListDocumentsResponse) Type() string                        { return "list_documents" }
func (r *ListDocumentsResponse) TimeCost() float64                   { return r.Duration }
func (r *ListDocumentsResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }
func (r *ListDocumentsResponse) PrintOut() {
	if r.outputFormat == OutputFormatJSON {
		raw, _ := json.MarshalIndent(r, "", "  ")
		fmt.Println(string(raw))
		return
	}
	fmt.Printf("Total: %d\n", r.Total)
	PrintTableSimpleByFormat(r.Docs, r.outputFormat)
}

func (c *MultiRAGClient) documentDatasetID(reference string) (string, error) {
	if id, err := uuid.Parse(reference); err == nil {
		if id.Version() != 1 {
			return "", fmt.Errorf("dataset ID must be UUID1")
		}
		return strings.ReplaceAll(id.String(), "-", ""), nil
	}
	match := ""
	for page := 1; page <= 100; page++ {
		query := url.Values{"keywords": {reference}, "page": {fmt.Sprint(page)}, "page_size": {"100"}}
		response, err := c.HTTPClient.RequestBounded("GET", "/datasets?"+query.Encode(), "api", nil, nil, 4*1024*1024)
		if err != nil {
			return "", err
		}
		if err := checkAuthResponse("resolve dataset", response); err != nil {
			return "", err
		}
		var envelope struct {
			Data *[]struct {
				ID   string `json:"id"`
				Name string `json:"name"`
			} `json:"data"`
			Total *int `json:"total_datasets"`
		}
		if err := json.Unmarshal(response.Body, &envelope); err != nil || envelope.Data == nil {
			return "", fmt.Errorf("invalid dataset list")
		}
		for _, dataset := range *envelope.Data {
			if dataset.Name != reference {
				continue
			}
			if match != "" {
				return "", fmt.Errorf("dataset name is ambiguous; use its ID")
			}
			id, err := uuid.Parse(dataset.ID)
			if err != nil || id.Version() != 1 {
				return "", fmt.Errorf("invalid resolved dataset ID")
			}
			match = strings.ReplaceAll(id.String(), "-", "")
		}
		if len(*envelope.Data) < 100 || envelope.Total != nil && *envelope.Total <= page*100 {
			if match == "" {
				return "", fmt.Errorf("dataset name not found")
			}
			return match, nil
		}
	}
	return "", fmt.Errorf("dataset name lookup exceeds limit; use its ID")
}

func (c *MultiRAGClient) ListDatasetDocuments(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" || c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("login in USER mode required")
	}
	reference, _ := cmd.Params["dataset"].(string)
	if strings.TrimSpace(reference) == "" {
		return nil, fmt.Errorf("dataset ID or name required")
	}
	id, err := c.documentDatasetID(reference)
	if err != nil {
		return nil, err
	}
	response, err := c.HTTPClient.RequestBounded("GET", "/datasets/"+url.PathEscape(id)+"/documents?page=1&page_size=10&return_empty_metadata=true", "api", nil, nil, 20*1024*1024)
	if err != nil {
		return nil, err
	}
	result, err := decodeDatasetDocuments(response)
	if err != nil {
		return nil, err
	}
	return result, nil
}

func decodeDatasetDocuments(response *Response) (*ListDocumentsResponse, error) {
	if err := checkAuthResponse("list documents", response); err != nil {
		return nil, err
	}
	var envelope struct {
		Data *struct {
			Docs     *[]map[string]any `json:"docs"`
			Total    *int64            `json:"total"`
			Page     int               `json:"page"`
			PageSize int               `json:"page_size"`
		} `json:"data"`
	}
	if err := json.Unmarshal(response.Body, &envelope); err != nil || envelope.Data == nil || envelope.Data.Docs == nil || envelope.Data.Total == nil {
		return nil, fmt.Errorf("missing document list or total")
	}
	d := envelope.Data
	if *d.Total < 0 || int64(len(*d.Docs)) > *d.Total {
		return nil, fmt.Errorf("invalid document total")
	}
	seen := map[string]bool{}
	for _, doc := range *d.Docs {
		id, ok := doc["id"].(string)
		if !ok || id == "" || seen[id] {
			return nil, fmt.Errorf("invalid document ID")
		}
		seen[id] = true
	}
	return &ListDocumentsResponse{Docs: *d.Docs, Total: *d.Total, Page: d.Page, PageSize: d.PageSize}, nil
}

func (p *Parser) parseListDatasetDocuments() (*Command, error) {
	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()
	reference, err := p.parseQuotedString()
	if err != nil || strings.TrimSpace(reference) == "" {
		return nil, fmt.Errorf("dataset ID or name required")
	}
	p.nextToken()
	if err := p.finishParseCommand(); err != nil {
		return nil, err
	}
	cmd := NewCommand("list_dataset_documents")
	cmd.Params["dataset"] = reference
	return cmd, nil
}
