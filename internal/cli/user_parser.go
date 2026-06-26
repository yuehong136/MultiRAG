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

import (
	"fmt"
	"strconv"
	"strings"
)

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
	case TokenModels:
		return p.parseListModelsOfProvider()
	case TokenProviders:
		return p.parseListProviders()
	case TokenInstances:
		return p.parseListInstances()
	case TokenDefault:
		return p.parseListDefaultModels()
	case TokenAvailable:
		return p.parseCommonListProviders()
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
		if p.curToken.Type == TokenUser {
			p.nextToken()
			if err := p.expectSemicolon(); err != nil {
				return nil, err
			}
			return NewCommand("show_current_user"), nil
		} else if p.curToken.Type == TokenModel {
			p.nextToken()
			if err := p.expectSemicolon(); err != nil {
				return nil, err
			}
			return NewCommand("show_current_model"), nil
		} else {
			return nil, fmt.Errorf("expected USER or MODEL after CURRENT")
		}
	case TokenProvider:
		return p.parseShowProvider()
	case TokenModel:
		return p.parseShowModel()
	case TokenInstance:
		return p.parseShowInstance()
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
	case TokenIndex:
		return p.parseCreateIndex()
	case TokenProvider:
		return p.parseCreateProviderInstance()
	default:
		return nil, fmt.Errorf("unknown CREATE target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseAddCommand() (*Command, error) {
	p.nextToken() // consume ADD
	switch p.curToken.Type {
	case TokenProvider:
		return p.parseAddProvider()
	default:
		return nil, fmt.Errorf("unknown ADD target: %s", p.curToken.Value)
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

// parseCreateIndex parses:
//
//	CREATE INDEX FOR DATASET 'name' VECTOR_SIZE N;
//	CREATE INDEX DOC_META;
func (p *Parser) parseCreateIndex() (*Command, error) {
	p.nextToken() // consume INDEX

	// Check if creating doc meta index
	if p.curToken.Type == TokenDocMeta {
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("create_doc_meta_index"), nil
	}

	// Otherwise, must be CREATE INDEX FOR DATASET 'name' VECTOR_SIZE N
	if p.curToken.Type != TokenFor {
		return nil, fmt.Errorf("expected FOR or DOC_META after INDEX, got %s", p.curToken.Value)
	}
	p.nextToken()

	if p.curToken.Type != TokenDataset {
		return nil, fmt.Errorf("expected DATASET after FOR, got %s", p.curToken.Value)
	}
	p.nextToken()

	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected dataset name, got %s", p.curToken.Value)
	}

	p.nextToken()
	if p.curToken.Type != TokenVectorSize {
		return nil, fmt.Errorf("expected VECTOR_SIZE after dataset name, got %s", p.curToken.Value)
	}
	p.nextToken()

	if p.curToken.Type != TokenNumber {
		return nil, fmt.Errorf("expected vector size number, got %s", p.curToken.Value)
	}
	vectorSize, err := strconv.Atoi(p.curToken.Value)
	if err != nil {
		return nil, fmt.Errorf("invalid vector size: %s", p.curToken.Value)
	}

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}

	cmd := NewCommand("create_index")
	cmd.Params["dataset_name"] = datasetName
	cmd.Params["vector_size"] = vectorSize
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
	case TokenIndex:
		return p.parseDropIndex()
	case TokenInstance:
		return p.parseDropInstance()
	default:
		return nil, fmt.Errorf("unknown DROP target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseDeleteCommand() (*Command, error) {
	p.nextToken() // consume DELETE

	switch p.curToken.Type {
	case TokenProvider:
		return p.parseDeleteProvider()
	default:
		return nil, fmt.Errorf("unknown DELETE target: %s", p.curToken.Value)
	}
}

// parseDropIndex parses:
//
//	DROP INDEX FOR DATASET 'name';
//	DROP INDEX DOC_META;
func (p *Parser) parseDropIndex() (*Command, error) {
	p.nextToken() // consume INDEX

	// Check if dropping doc meta index
	if p.curToken.Type == TokenDocMeta {
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("drop_doc_meta_index"), nil
	}

	// Otherwise, must be DROP INDEX FOR DATASET 'name'
	if p.curToken.Type != TokenFor {
		return nil, fmt.Errorf("expected FOR or DOC_META after INDEX, got %s", p.curToken.Value)
	}
	p.nextToken()

	if p.curToken.Type != TokenDataset {
		return nil, fmt.Errorf("expected DATASET after FOR, got %s", p.curToken.Value)
	}
	p.nextToken()

	datasetName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected dataset name, got %s", p.curToken.Value)
	}

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_index")
	cmd.Params["dataset_name"] = datasetName
	return cmd, nil
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

// ==================== User PROVIDER ====================

// parseListProviders parses: LIST PROVIDERS;
func (p *Parser) parseListProviders() (*Command, error) {
	p.nextToken() // consume PROVIDERS
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return NewCommand("list_providers"), nil
}

// parseAddProvider parses:
//
//	ADD PROVIDER '<name>';
//	ADD PROVIDER '<name>' '<api_key>';
func (p *Parser) parseAddProvider() (*Command, error) {
	p.nextToken() // consume PROVIDER

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}

	cmd := NewCommand("add_provider")
	cmd.Params["provider_name"] = providerName

	p.nextToken()

	// Optional api_key
	if p.curToken.Type == TokenQuotedString {
		apiKey, err := p.parseQuotedString()
		if err != nil {
			return nil, fmt.Errorf("expected api key: %w", err)
		}
		cmd.Params["api_key"] = apiKey
		p.nextToken()
	}

	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseDeleteProvider parses: DELETE PROVIDER '<name>';
func (p *Parser) parseDeleteProvider() (*Command, error) {
	p.nextToken() // consume PROVIDER

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}

	cmd := NewCommand("delete_provider")
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseUserAlterCommand dispatches user-mode ALTER commands.
func (p *Parser) parseUserAlterCommand() (*Command, error) {
	p.nextToken() // consume ALTER

	switch p.curToken.Type {
	case TokenProvider:
		return p.parseAlterProvider()
	case TokenInstance:
		return p.parseAlterInstance()
	default:
		return nil, fmt.Errorf("unknown ALTER target: %s", p.curToken.Value)
	}
}

// parseAlterProvider parses: ALTER PROVIDER '<name>' NAME '<new_name>';
func (p *Parser) parseAlterProvider() (*Command, error) {
	p.nextToken() // consume PROVIDER

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenName {
		return nil, fmt.Errorf("expected NAME")
	}
	p.nextToken()

	newName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected new provider name: %w", err)
	}

	cmd := NewCommand("alter_provider")
	cmd.Params["provider_name"] = providerName
	cmd.Params["new_name"] = newName

	p.nextToken()
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
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

// ==================== Internal CLI for GO ====================

// parseInsertCommand parses INSERT command and dispatches to specific handler
func (p *Parser) parseInsertCommand() (*Command, error) {
	p.nextToken() // consume INSERT

	// Expect DATASET or METADATA
	if p.curToken.Type == TokenDataset {
		return p.parseInsertDatasetFromFile()
	}
	if p.curToken.Type == TokenMetadata {
		return p.parseInsertMetadataFromFile()
	}
	return nil, fmt.Errorf("expected DATASET or METADATA after INSERT, got %s", p.curToken.Value)
}

// parseInsertDatasetFromFile parses: INSERT DATASET FROM FILE "file_path"
func (p *Parser) parseInsertDatasetFromFile() (*Command, error) {
	p.nextToken() // consume DATASET

	// Expect FROM
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM, got %s", p.curToken.Value)
	}
	p.nextToken()

	// Expect FILE
	if p.curToken.Type != TokenFile {
		return nil, fmt.Errorf("expected FILE, got %s", p.curToken.Value)
	}
	p.nextToken()

	// Get file path (quoted string)
	filePath, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("insert_dataset_from_file")
	cmd.Params["file_path"] = filePath

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseInsertMetadataFromFile parses: INSERT METADATA FROM FILE "file_path"
func (p *Parser) parseInsertMetadataFromFile() (*Command, error) {
	p.nextToken() // consume METADATA

	// Expect FROM
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM, got %s", p.curToken.Value)
	}
	p.nextToken()

	// Expect FILE
	if p.curToken.Type != TokenFile {
		return nil, fmt.Errorf("expected FILE, got %s", p.curToken.Value)
	}
	p.nextToken()

	// Get file path (quoted string)
	filePath, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("insert_metadata_from_file")
	cmd.Params["file_path"] = filePath

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

func (p *Parser) parseCreateProviderInstance() (*Command, error) {
	p.nextToken() // consume PROVIDER

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenInstance {
		return nil, fmt.Errorf("expected INSTANCE after provider name")
	}
	p.nextToken()

	instanceName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected instance name: %w", err)
	}

	// Check if instance_name is "default"
	if instanceName == "default" {
		return nil, fmt.Errorf("instance name cannot be 'default'")
	}

	p.nextToken()
	apiKey, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected API key: %w", err)
	}

	cmd := NewCommand("create_provider_instance")
	cmd.Params["provider_name"] = providerName
	cmd.Params["instance_name"] = instanceName
	cmd.Params["api_key"] = apiKey

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseListInstances parses LIST INSTANCES FROM PROVIDER <name> command

func (p *Parser) parseListInstances() (*Command, error) {
	p.nextToken() // consume INSTANCES

	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name after FROM PROVIDER: %w", err)
	}

	cmd := NewCommand("list_provider_instances")
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseShowInstance parses SHOW INSTANCE <name> FROM PROVIDER <name> command

func (p *Parser) parseShowInstance() (*Command, error) {
	p.nextToken() // consume INSTANCE

	instanceName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected instance name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name after FROM PROVIDER: %w", err)
	}

	cmd := NewCommand("show_provider_instance")
	cmd.Params["instance_name"] = instanceName
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseAlterInstance parses ALTER INSTANCE <name> NAME <new_name> FROM PROVIDER <name> command

func (p *Parser) parseAlterInstance() (*Command, error) {
	p.nextToken() // consume INSTANCE

	instanceName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected instance name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenName {
		return nil, fmt.Errorf("expected NAME")
	}
	p.nextToken()

	newName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected new instance name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	if p.curToken.Type != TokenProvider {
		return nil, fmt.Errorf("expected PROVIDER after FROM")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name after FROM PROVIDER: %w", err)
	}

	cmd := NewCommand("alter_provider_instance")
	cmd.Params["instance_name"] = instanceName
	cmd.Params["new_name"] = newName
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseDropInstance parses DROP INSTANCE <name> FROM PROVIDER <name> command

func (p *Parser) parseDropInstance() (*Command, error) {
	p.nextToken() // consume INSTANCE

	instanceName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected instance name: %w", err)
	}

	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name after FROM PROVIDER: %w", err)
	}

	cmd := NewCommand("drop_provider_instance")
	cmd.Params["instance_name"] = instanceName
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

func (p *Parser) parseEnableCommand() (*Command, error) {
	p.nextToken() // consume ENABLE

	if p.curToken.Type != TokenModel {
		return nil, fmt.Errorf("expected MODEL")
	}
	p.nextToken()

	modelName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	modelProvider, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	modelInstance, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	// Semicolon is optional for UNSET TOKEN
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	cmd := NewCommand("enable_model")
	cmd.Params["model_name"] = modelName
	cmd.Params["instance_name"] = modelInstance
	cmd.Params["provider_name"] = modelProvider
	return cmd, nil
}

func (p *Parser) parseDisableCommand() (*Command, error) {
	p.nextToken() // consume DISABLE

	if p.curToken.Type != TokenModel {
		return nil, fmt.Errorf("expected MODEL")
	}
	p.nextToken()

	modelName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	modelProvider, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	modelInstance, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	// Semicolon is optional for UNSET TOKEN
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	cmd := NewCommand("disable_model")
	cmd.Params["model_name"] = modelName
	cmd.Params["instance_name"] = modelInstance
	cmd.Params["provider_name"] = modelProvider
	return cmd, nil
}

func (p *Parser) parseChatCommand() (*Command, error) {
	p.nextToken() // consume CHAT

	var modelName string
	var message string

	// Check if we have a quoted string that looks like a model identifier (contains two slashes)
	// Format: 'provider/instance/model' or just 'message'
	if p.curToken.Type == TokenQuotedString {
		firstArg := p.curToken.Value

		// Check if it looks like a model identifier (contains exactly 2 slashes)
		slashCount := strings.Count(firstArg, "/")
		if slashCount == 2 {
			// This is likely a model identifier, expect another quoted string for message
			modelName = firstArg
			p.nextToken()

			// After model name, expect message
			if p.curToken.Type != TokenQuotedString {
				return nil, fmt.Errorf("expected message after model name")
			}
			message = p.curToken.Value
			p.nextToken()
		} else {
			// This is just a message, use current model
			message = firstArg
			p.nextToken()
		}
	} else if p.curToken.Type == TokenIdentifier {
		// Context engine style: chat <message>
		message = p.curToken.Value
		p.nextToken()
	} else {
		return nil, fmt.Errorf("expected model name (quoted string) or message")
	}

	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	cmd := NewCommand("chat_to_model")
	if modelName != "" {
		cmd.Params["model_name"] = modelName
	}
	cmd.Params["message"] = message
	cmd.Params["reasoning"] = false
	return cmd, nil
}

func (p *Parser) parseThinkCommand() (*Command, error) {
	p.nextToken() // consume THINK
	command, err := p.parseChatCommand()
	if err != nil {
		return nil, err
	}
	command.Type = "think_chat_to_model"
	command.Params["reasoning"] = true
	return command, nil
}

func (p *Parser) parseUseCommand() (*Command, error) {
	p.nextToken() // consume USE

	if p.curToken.Type != TokenModel {
		return nil, fmt.Errorf("expected MODEL after USE")
	}
	p.nextToken() // consume MODEL

	// Parse model identifier in format 'provider/instance/model'
	modelIdentifier, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected model identifier in format 'provider/instance/model': %w", err)
	}
	p.nextToken()

	// Semicolon is optional
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	cmd := NewCommand("use_model")
	cmd.Params["model_identifier"] = modelIdentifier
	return cmd, nil
}
