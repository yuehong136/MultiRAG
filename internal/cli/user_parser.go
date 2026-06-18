//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package cli

import "fmt"

// ==================== User REGISTER ====================

func (p *Parser) parseRegisterCommand() (*Command, error) {
	cmd := NewCommand("register_user")

	p.nextToken() // consume REGISTER
	if err := p.expectPeek(TokenUser); err != nil {
		return nil, err
	}
	p.nextToken()

	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	cmd.Params["user_name"] = userName

	p.nextToken()
	if p.curToken.Type != TokenAs {
		return nil, fmt.Errorf("expected AS")
	}

	p.nextToken()
	nickname, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	cmd.Params["nickname"] = nickname

	p.nextToken()
	if p.curToken.Type != TokenPassword {
		return nil, fmt.Errorf("expected PASSWORD")
	}

	p.nextToken()
	password, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	cmd.Params["password"] = password

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}

	return cmd, nil
}

// ==================== User LIST ====================

func (p *Parser) parseListCommand() (*Command, error) {
	p.nextToken() // consume LIST

	switch p.curToken.Type {
	case TokenDatasets:
		return p.parseListDatasets()
	case TokenAgents:
		return p.parseListAgents()
	case TokenTokens:
		return p.parseListTokens()
	case TokenModel:
		return p.parseListModelProviders()
	case TokenDefault:
		return p.parseListDefaultModels()
	case TokenChats:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_user_chats"), nil
	case TokenFiles:
		return p.parseListFiles()
	default:
		return nil, fmt.Errorf("unknown LIST target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseListModelProviders() (*Command, error) {
	p.nextToken() // consume MODEL
	if p.curToken.Type != TokenProviders {
		return nil, fmt.Errorf("expected PROVIDERS")
	}
	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return NewCommand("list_user_model_providers"), nil
}

func (p *Parser) parseListDefaultModels() (*Command, error) {
	p.nextToken() // consume DEFAULT
	if p.curToken.Type != TokenModels {
		return nil, fmt.Errorf("expected MODELS")
	}
	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return NewCommand("list_user_default_models"), nil
}

func (p *Parser) parseListFiles() (*Command, error) {
	p.nextToken() // consume FILES
	if p.curToken.Type != TokenOf {
		return nil, fmt.Errorf("expected OF")
	}
	p.nextToken()
	if p.curToken.Type != TokenDataset {
		return nil, fmt.Errorf("expected DATASET")
	}
	p.nextToken()

	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("list_user_dataset_files")
	cmd.Params["dataset_name"] = datasetName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== User SHOW ====================

func (p *Parser) parseShowCommand() (*Command, error) {
	p.nextToken() // consume SHOW

	switch p.curToken.Type {
	case TokenVersion:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("show_version"), nil
	case TokenCurrent:
		p.nextToken()
		if p.curToken.Type != TokenUser {
			return nil, fmt.Errorf("expected USER after CURRENT")
		}
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("show_current_user"), nil
	default:
		return nil, fmt.Errorf("unknown SHOW target: %s", p.curToken.Value)
	}
}

// ==================== User CREATE ====================

func (p *Parser) parseCreateCommand() (*Command, error) {
	p.nextToken() // consume CREATE

	switch p.curToken.Type {
	case TokenDataset:
		return p.parseCreateDataset()
	case TokenChat:
		return p.parseCreateChat()
	case TokenToken:
		return p.parseCreateToken()
	default:
		return nil, fmt.Errorf("unknown CREATE target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseCreateDataset() (*Command, error) {
	p.nextToken() // consume DATASET
	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenWith {
		return nil, fmt.Errorf("expected WITH")
	}
	p.nextToken()
	if p.curToken.Type != TokenEmbedding {
		return nil, fmt.Errorf("expected EMBEDDING")
	}
	p.nextToken()

	embedding, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	cmd := NewCommand("create_user_dataset")
	cmd.Params["dataset_name"] = datasetName
	cmd.Params["embedding"] = embedding

	if p.curToken.Type == TokenParser {
		p.nextToken()
		parserType, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["parser_type"] = parserType
		p.nextToken()
	} else if p.curToken.Type == TokenPipeline {
		p.nextToken()
		pipeline, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["pipeline"] = pipeline
		p.nextToken()
	} else {
		return nil, fmt.Errorf("expected PARSER or PIPELINE")
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseCreateChat() (*Command, error) {
	p.nextToken() // consume CHAT
	chatName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("create_user_chat")
	cmd.Params["chat_name"] = chatName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseCreateToken parses: CREATE TOKEN;  (user mode, self-service)
func (p *Parser) parseCreateToken() (*Command, error) {
	p.nextToken() // consume TOKEN
	cmd := NewCommand("create_token")
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== User DROP ====================

func (p *Parser) parseDropCommand() (*Command, error) {
	p.nextToken() // consume DROP

	switch p.curToken.Type {
	case TokenDataset:
		return p.parseDropDataset()
	case TokenChat:
		return p.parseDropChat()
	case TokenToken:
		return p.parseDropToken()
	default:
		return nil, fmt.Errorf("unknown DROP target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseDropDataset() (*Command, error) {
	p.nextToken() // consume DATASET
	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_user_dataset")
	cmd.Params["dataset_name"] = datasetName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseDropChat() (*Command, error) {
	p.nextToken() // consume CHAT
	chatName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_user_chat")
	cmd.Params["chat_name"] = chatName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== User SET / RESET (default models) ====================

func (p *Parser) parseSetCommand() (*Command, error) {
	p.nextToken() // consume SET

	if p.curToken.Type == TokenDefault {
		return p.parseSetDefault()
	}

	return nil, fmt.Errorf("unknown SET target: %s", p.curToken.Value)
}

func (p *Parser) parseSetDefault() (*Command, error) {
	p.nextToken() // consume DEFAULT

	var modelType, modelID string

	switch p.curToken.Type {
	case TokenLLM:
		modelType = "llm_id"
	case TokenVLM:
		modelType = "img2txt_id"
	case TokenEmbedding:
		modelType = "embd_id"
	case TokenReranker:
		modelType = "reranker_id"
	case TokenASR:
		modelType = "asr_id"
	case TokenTTS:
		modelType = "tts_id"
	default:
		return nil, fmt.Errorf("unknown model type: %s", p.curToken.Value)
	}

	p.nextToken()
	id, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	modelID = id

	cmd := NewCommand("set_default_model")
	cmd.Params["model_type"] = modelType
	cmd.Params["model_id"] = modelID

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseResetCommand() (*Command, error) {
	p.nextToken() // consume RESET

	if p.curToken.Type != TokenDefault {
		return nil, fmt.Errorf("expected DEFAULT")
	}
	p.nextToken()

	var modelType string
	switch p.curToken.Type {
	case TokenLLM:
		modelType = "llm_id"
	case TokenVLM:
		modelType = "img2txt_id"
	case TokenEmbedding:
		modelType = "embd_id"
	case TokenReranker:
		modelType = "reranker_id"
	case TokenASR:
		modelType = "asr_id"
	case TokenTTS:
		modelType = "tts_id"
	default:
		return nil, fmt.Errorf("unknown model type: %s", p.curToken.Value)
	}

	cmd := NewCommand("reset_default_model")
	cmd.Params["model_type"] = modelType

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== User IMPORT / SEARCH / PARSE ====================

func (p *Parser) parseImportCommand() (*Command, error) {
	p.nextToken() // consume IMPORT
	documentPaths, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenInto {
		return nil, fmt.Errorf("expected INTO")
	}
	p.nextToken()
	if p.curToken.Type != TokenDataset {
		return nil, fmt.Errorf("expected DATASET")
	}
	p.nextToken()

	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("import_docs_into_dataset")
	cmd.Params["document_paths"] = documentPaths
	cmd.Params["dataset_name"] = datasetName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseSearchCommand() (*Command, error) {
	p.nextToken() // consume SEARCH
	question, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenOn {
		return nil, fmt.Errorf("expected ON")
	}
	p.nextToken()
	if p.curToken.Type != TokenDatasets {
		return nil, fmt.Errorf("expected DATASETS")
	}
	p.nextToken()

	datasets, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("search_on_datasets")
	cmd.Params["question"] = question
	cmd.Params["datasets"] = datasets

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseParseCommand() (*Command, error) {
	p.nextToken() // consume PARSE

	if p.curToken.Type == TokenDataset {
		return p.parseParseDataset()
	}

	return p.parseParseDocs()
}

func (p *Parser) parseParseDataset() (*Command, error) {
	p.nextToken() // consume DATASET
	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	var method string
	if p.curToken.Type == TokenSync {
		method = "sync"
	} else if p.curToken.Type == TokenAsync {
		method = "async"
	} else {
		return nil, fmt.Errorf("expected SYNC or ASYNC")
	}

	cmd := NewCommand("parse_dataset")
	cmd.Params["dataset_name"] = datasetName
	cmd.Params["method"] = method

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseParseDocs() (*Command, error) {
	documentNames, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenOf {
		return nil, fmt.Errorf("expected OF")
	}
	p.nextToken()
	if p.curToken.Type != TokenDataset {
		return nil, fmt.Errorf("expected DATASET")
	}
	p.nextToken()

	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("parse_dataset_docs")
	cmd.Params["document_names"] = documentNames
	cmd.Params["dataset_name"] = datasetName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}
