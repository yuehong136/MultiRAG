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

package service

import (
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/common"
	"multirag/internal/server"
	"multirag/internal/utility"
	"net/http"
	"time"

	"go.uber.org/zap"
)

const maxHeartbeatResponseBytes = 64 << 10

// HeartbeatSender is responsible for sending heartbeat reports to the admin server
type HeartbeatSender struct {
	client       *utility.HTTPClient
	logger       *zap.Logger
	serverType   common.ServerType
	serverName   string
	host         string
	port         int
	version      string
	lastSuccess  bool
	attemptCount int
}

// NewHeartbeatSender creates a new heartbeat service instance
func NewHeartbeatSender(logger *zap.Logger, serverType common.ServerType, serverName, host string, port int) *HeartbeatSender {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &HeartbeatSender{
		logger:       logger,
		serverType:   serverType,
		serverName:   serverName,
		host:         host,
		port:         port,
		version:      utility.GetMultiRAGVersion(),
		lastSuccess:  false,
		attemptCount: 0,
	}
}

// InitHTTPClient initializes the HTTP client with admin server configuration
func (h *HeartbeatSender) InitHTTPClient() error {
	if h == nil {
		return fmt.Errorf("heartbeat sender is unavailable")
	}
	if h.logger == nil {
		h.logger = zap.NewNop()
	}
	adminConfig := server.GetAdminConfig()
	if adminConfig == nil {
		return fmt.Errorf("admin configuration not found")
	}

	h.client = utility.NewHTTPClientBuilder().
		WithHost(adminConfig.Host).
		WithPort(adminConfig.Port).
		WithTimeout(10 * time.Second).
		Build()

	h.logger.Info("Heartbeat HTTP client initialized",
		zap.String("admin_host", adminConfig.Host),
		zap.Int("admin_port", adminConfig.Port),
	)

	return nil
}

// SendHeartbeat sends a heartbeat message to the admin server
func (h *HeartbeatSender) SendHeartbeat() error {
	if h == nil {
		return fmt.Errorf("heartbeat sender is unavailable")
	}
	if h.logger == nil {
		h.logger = zap.NewNop()
	}

	if h.attemptCount < 10 {
		if h.lastSuccess {
			h.attemptCount++
			return nil
		}
	}
	h.attemptCount = 0
	h.lastSuccess = false

	if h.client == nil {
		if err := h.InitHTTPClient(); err != nil {
			h.logger.Error("Failed to initialize HTTP client", zap.Error(err))
			return err
		}
	}

	message := &common.BaseMessage{
		MessageID:   time.Now().UnixNano(),
		MessageType: common.MessageHeartbeat,
		ServerName:  h.serverName,
		ServerType:  h.serverType,
		Host:        h.host,
		Port:        h.port,
		Version:     h.version,
		Timestamp:   time.Now(),
		Ext:         nil,
	}

	jsonData, err := json.Marshal(message)
	if err != nil {
		h.logger.Error("Failed to marshal heartbeat message", zap.Error(err))
		return err
	}

	resp, err := h.client.PostJSON("/api/v1/admin/reports", jsonData)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	// Decode into an integer pointer: omitted/null, strings and fractional codes
	// are protocol errors, never license success. Non-200 is always a failure.
	var responseBody struct {
		Code *int `json:"code"`
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxHeartbeatResponseBytes+1))
	if err != nil {
		return fmt.Errorf("unreadable heartbeat response (status %d)", resp.StatusCode)
	}
	if len(body) > maxHeartbeatResponseBytes {
		return fmt.Errorf("heartbeat response exceeds size limit (status %d)", resp.StatusCode)
	}
	if err := json.Unmarshal(body, &responseBody); err != nil {
		return fmt.Errorf("invalid heartbeat response (status %d)", resp.StatusCode)
	}
	if responseBody.Code == nil {
		return fmt.Errorf("heartbeat response (status %d) is missing code", resp.StatusCode)
	}
	responseCode := common.ErrorCode(*responseBody.Code)
	if resp.StatusCode != http.StatusOK || responseCode != common.CodeLicenseValid {
		return fmt.Errorf("heartbeat rejected (status %d, code %d): %s", resp.StatusCode, responseCode, responseCode.Message())
	}

	h.logger.Debug("Heartbeat sent successfully",
		zap.String("server_id", h.serverName),
		zap.String("server_type", string(h.serverType)),
	)

	h.lastSuccess = true

	return nil
}
