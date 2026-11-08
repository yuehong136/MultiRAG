package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/ingestion"
	"multirag/internal/ingestion/parser"
	"os"
)

func readLocalInput(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("local input exceeds %d bytes", limit)
	}
	return data, nil
}
func (p *Parser) parseLocalFileCommand() (*Command, error) {
	p.nextToken()
	filename, err := p.parseQuotedString()
	if err != nil || filename == "" {
		return nil, fmt.Errorf("local filename required")
	}
	p.nextToken()
	cmd := NewCommand("user_parse_local_file_command")
	cmd.Params["file"] = filename
	if p.curToken.Type == TokenWith {
		p.nextToken()
		config, err := p.parseQuotedString()
		if err != nil || config == "" {
			return nil, fmt.Errorf("config filename required")
		}
		cmd.Params["config"] = config
		p.nextToken()
	}
	if err := p.finishParseCommand(); err != nil {
		return nil, err
	}
	return cmd, nil
}
func (p *Parser) parseLocalChunkCommand() (*Command, error) {
	explain := p.curToken.Type == TokenExplain
	p.nextToken()
	if explain {
		if p.curToken.Type != TokenChunk {
			return nil, fmt.Errorf("expected CHUNK")
		}
		p.nextToken()
	}
	filename, err := p.parseQuotedString()
	if err != nil || filename == "" {
		return nil, fmt.Errorf("filename required")
	}
	p.nextToken()
	if p.curToken.Type != TokenWith {
		return nil, fmt.Errorf("expected WITH")
	}
	p.nextToken()
	dsl, err := p.parseQuotedString()
	if err != nil || dsl == "" {
		return nil, fmt.Errorf("DSL filename required")
	}
	p.nextToken()
	if err := p.finishParseCommand(); err != nil {
		return nil, err
	}
	kind := "user_chunk_command"
	if explain {
		kind = "user_explain_chunk_command"
	}
	cmd := NewCommand(kind)
	cmd.Params["file"], cmd.Params["dsl"] = filename, dsl
	return cmd, nil
}
func (c *MultiRAGClient) LocalParseCommand(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("local parsing requires USER mode")
	}
	filename, _ := cmd.Params["file"].(string)
	config := map[string]string{"lib_type": "builtin"}
	if path, ok := cmd.Params["config"].(string); ok {
		data, err := readLocalInput(path, 1<<20)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(data, &config); err != nil || config == nil {
			return nil, fmt.Errorf("parser config must be a JSON string map")
		}
	}
	data, err := readLocalInput(filename, 32<<20)
	if err != nil {
		return nil, err
	}
	result, err := parser.Parse(filename, data, config)
	if err != nil {
		return nil, err
	}
	return &CommonDataResponse{Data: map[string]any{"format": result.Format, "text": result.Text, "markdown": result.Markdown, "backend": result.Backend}}, nil
}
func (c *MultiRAGClient) LocalChunkCommand(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("local chunking requires USER mode")
	}
	filename, _ := cmd.Params["file"].(string)
	dsl, _ := cmd.Params["dsl"].(string)
	raw, err := readLocalInput(dsl, 1<<20)
	if err != nil {
		return nil, err
	}
	engine := ingestion.NewChunkEngine()
	plan, err := engine.Compile(string(raw))
	if err != nil {
		return nil, err
	}
	if cmd.Type == "user_explain_chunk_command" {
		text, err := engine.Explain(plan)
		if err != nil {
			return nil, err
		}
		return &CommonDataResponse{Data: map[string]any{"explanation": text}}, nil
	}
	raw, err = readLocalInput(filename, 32<<20)
	if err != nil {
		return nil, err
	}
	result, err := engine.Execute(plan, string(raw))
	if err != nil {
		return nil, err
	}
	rows := make([]map[string]any, 0, len(result.ResultChunks))
	for _, chunk := range result.ResultChunks {
		rows = append(rows, map[string]any{"content": chunk.Content, "size": chunk.Size, "index": chunk.Index, "metadata": chunk.Metadata})
	}
	return &CommonResponse{Data: rows}, nil
}
