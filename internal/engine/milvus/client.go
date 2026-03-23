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

package milvus

import (
	"context"
	"fmt"
	"strings"

	"github.com/milvus-io/milvus/client/v2/milvusclient"
	"go.uber.org/zap"

	"multirag/internal/logger"
	"multirag/internal/server"
)

// milvusEngine Milvus engine implementation
type milvusEngine struct {
	client *milvusclient.Client
	config *server.MilvusConfig
}

// NewEngine creates a Milvus engine
func NewEngine(cfg interface{}) (*milvusEngine, error) {
	milvusConfig, ok := cfg.(*server.MilvusConfig)
	if !ok || milvusConfig == nil {
		return nil, fmt.Errorf("invalid Milvus config type, expected *server.MilvusConfig")
	}

	// Parse hosts URL to get address
	addr := milvusConfig.Hosts
	addr = strings.TrimPrefix(addr, "http://")
	addr = strings.TrimPrefix(addr, "https://")

	ctx := context.Background()
	cli, err := milvusclient.New(ctx, &milvusclient.ClientConfig{
		Address:  addr,
		Username: milvusConfig.Username,
		Password: milvusConfig.Password,
		DBName:   milvusConfig.DBName,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to connect to Milvus: %w", err)
	}

	logger.Info("Milvus engine connected", zap.String("address", addr))
	return &milvusEngine{client: cli, config: milvusConfig}, nil
}

// Type returns the engine type
func (e *milvusEngine) Type() string {
	return "milvus"
}

// Ping health check
func (e *milvusEngine) Ping(ctx context.Context) error {
	// List collections as a health check
	_, err := e.client.ListCollections(ctx, milvusclient.NewListCollectionOption())
	return err
}

// Close closes the Milvus connection
func (e *milvusEngine) Close() error {
	return e.client.Close(context.Background())
}
