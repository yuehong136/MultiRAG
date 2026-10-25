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
	"context"
	"errors"
	"fmt"
	"multirag/internal/cache"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/utility"
	"time"
)

// SystemService system service
type SystemService struct {
	healthChecks map[string]func(context.Context) error
}

// NewSystemService create system service
func NewSystemService() *SystemService {
	return &SystemService{}
}

// ConfigResponse system configuration response
type ConfigResponse struct {
	RegisterEnabled      int  `json:"registerEnabled"`
	DisablePasswordLogin bool `json:"disablePasswordLogin"`
}

// GetConfig get system configuration
func (s *SystemService) GetConfig() (*ConfigResponse, error) {
	cfg := server.GetConfig()
	return &ConfigResponse{
		RegisterEnabled:      cfg.RegisterEnabled,
		DisablePasswordLogin: cfg.Authentication.DisablePasswordLogin,
	}, nil
}

// VersionResponse version response
type VersionResponse struct {
	Version string `json:"version"`
}

// GetVersion get MultiRAG version
func (s *SystemService) GetVersion() (*VersionResponse, error) {
	version := utility.GetMultiRAGVersion()
	return &VersionResponse{
		Version: version,
	}, nil
}

// HealthzMeta includes timing and sanitized dependency failures.
type HealthzMeta struct {
	Elapsed string `json:"elapsed"`
	Reason  string `json:"reason,omitempty"`
	Error   string `json:"error,omitempty"`
}
type HealthzResponse struct {
	DB        string                 `json:"db"`
	Redis     string                 `json:"redis"`
	DocEngine string                 `json:"doc_engine"`
	Storage   string                 `json:"storage"`
	Status    string                 `json:"status"`
	Meta      map[string]HealthzMeta `json:"_meta,omitempty"`
}

// Healthz reports every required dependency; /health remains a liveness endpoint.
func (s *SystemService) Healthz(ctx context.Context) (*HealthzResponse, bool) {
	checks := s.healthChecks
	if checks == nil {
		checks = map[string]func(context.Context) error{
			"db": func(ctx context.Context) error {
				if dao.DB == nil {
					return errHealthUninitialized
				}
				db, err := dao.DB.DB()
				if err != nil {
					return err
				}
				return db.PingContext(ctx)
			},
			"redis": func(ctx context.Context) error {
				if cache.Get() == nil {
					return errHealthUninitialized
				}
				return cache.Get().PingContext(ctx)
			},
			"doc_engine": func(ctx context.Context) error { return documentEngineHealth(ctx, engine.Get()) },
			"storage": func(ctx context.Context) error {
				if storage.GetStorageFactory().GetStorage() == nil {
					return errHealthUninitialized
				}
				return storage.Readiness(ctx)
			},
		}
	}
	states := map[string]string{}
	meta := map[string]HealthzMeta{}
	allOK := true
	for _, key := range []string{"db", "redis", "doc_engine", "storage"} {
		start := time.Now()
		bounded, cancel := context.WithTimeout(ctx, 3*time.Second)
		check := checks[key]
		var err error
		if check == nil {
			err = errHealthUninitialized
		} else {
			err = check(bounded)
		}
		if bounded.Err() != nil {
			err = bounded.Err()
		}
		cancel()
		states[key] = "ok"
		m := HealthzMeta{Elapsed: fmt.Sprintf("%.1f", float64(time.Since(start).Microseconds())/1000)}
		if err != nil {
			allOK = false
			states[key] = "nok"
			m.Error = key + " health check failed"
			m.Reason = "probe_failed"
			switch {
			case errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
				m.Reason = "timeout"
			case errors.Is(err, errHealthUnsupported):
				m.Reason = "unsupported"
			case errors.Is(err, errHealthUninitialized):
				m.Reason = "uninitialized"
			}
		}
		meta[key] = m
	}
	status := "ok"
	if !allOK {
		status = "nok"
	}
	return &HealthzResponse{DB: states["db"], Redis: states["redis"], DocEngine: states["doc_engine"], Storage: states["storage"], Status: status, Meta: meta}, allOK
}

var errHealthUnsupported = errors.New("active health probe unavailable")
var errHealthUninitialized = errors.New("dependency not initialized")

func documentEngineHealth(ctx context.Context, store engine.DocEngine) error {
	if store == nil {
		return errHealthUninitialized
	}
	if kind, ok := store.(interface{ Type() string }); ok && kind.Type() == "infinity" {
		return errHealthUnsupported
	}
	return store.Ping(ctx)
}
