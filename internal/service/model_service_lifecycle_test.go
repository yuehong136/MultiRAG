package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

func TestCanonicalModelWritesFenceTenantRemoval(t *testing.T) {
	s := canonicalModelDatabase(t)
	svc := NewModelProviderService()
	for _, enrollment := range []struct{ provider, user string }{{"OpenAI", "model-owner"}, {"DeepSeek", "model-other"}} {
		if code, err := svc.AddModelProvider(enrollment.provider, enrollment.user); err != nil || code != common.CodeSuccess {
			t.Fatal("seed provider", code, err)
		}
	}
	var provider entity.TenantModelProvider
	if err := s.DB.Where("tenant_id = ? AND provider_name = ?", "model-owner", "OpenAI").First(&provider).Error; err != nil {
		t.Fatal(err)
	}

	// The removal holds the real tenant row lock while both actual service
	// writers reach their tenant read. The callback only exposes that boundary;
	// it neither blocks the writers nor supplies a query result.
	removal := s.DB.Begin()
	if removal.Error != nil {
		t.Fatal(removal.Error)
	}
	defer removal.Rollback()
	var tenant entity.Tenant
	if err := removal.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", "model-owner").First(&tenant).Error; err != nil {
		t.Fatal(err)
	}
	original := dao.DB
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	dao.DB = dao.DB.WithContext(ctx)
	defer func() { dao.DB = original }()
	attempts := make(chan struct{}, 2)
	callback := "model-tenant-removal-" + s.Prefix
	if err := dao.DB.Callback().Query().Before("gorm:query").Register(callback, func(tx *gorm.DB) {
		if tx.Statement.Schema != nil && tx.Statement.Schema.Table == "tenant" {
			select {
			case attempts <- struct{}{}:
			default:
			}
		}
	}); err != nil {
		t.Fatal(err)
	}
	defer dao.DB.Callback().Query().Remove(callback)
	type outcome struct {
		operation string
		code      common.ErrorCode
		err       error
	}
	results := make(chan outcome, 2)
	go func() {
		code, err := svc.AddModelProvider("DeepSeek", "model-owner")
		results <- outcome{"provider", code, err}
	}()
	go func() {
		code, err := svc.CreateProviderInstance("OpenAI", "late", "controlled-late-credential", "http://unused.invalid", "default", "model-owner")
		results <- outcome{"instance", code, err}
	}()
	for range 2 {
		select {
		case <-attempts:
		case <-ctx.Done():
			t.Fatal("writers did not reach the tenant boundary")
		}
	}
	select {
	case result := <-results:
		t.Fatal("writer bypassed the removal lock", result.operation, result.code, result.err)
	case <-time.After(100 * time.Millisecond):
	}
	if err := removal.Where("tenant_id = ?", "model-owner").Delete(&entity.TenantModelProvider{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := removal.Where("id = ?", "model-owner").Delete(&entity.Tenant{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := removal.Commit().Error; err != nil {
		t.Fatal(err)
	}
	for range 2 {
		select {
		case result := <-results:
			if result.err == nil || result.code != common.CodeNotFound || errors.Is(result.err, context.DeadlineExceeded) || errors.Is(result.err, context.Canceled) {
				t.Fatal("late writer was not rejected by the deleted tenant", result.operation, result.code, result.err)
			}
		case <-ctx.Done():
			t.Fatal("late writer did not finish after removal committed")
		}
	}
	// Intentionally retain the stale active owner relationship: authorization
	// must check the tenant itself rather than succeeding through that row.
	for query, argument := range map[string]string{
		"SELECT count(*) FROM tenant_model_provider WHERE tenant_id = ?":   "model-owner",
		"SELECT count(*) FROM tenant_model_instance WHERE provider_id = ?": provider.ID,
	} {
		var count int
		if err := s.Read.QueryRowContext(t.Context(), query, argument).Scan(&count); err != nil || count != 0 {
			t.Fatal("late model state survived independent SQL readback", count, err)
		}
	}
	var relationships, foreign int
	if err := s.Read.QueryRowContext(t.Context(), "SELECT count(*) FROM user_tenant WHERE tenant_id = ?", "model-owner").Scan(&relationships); err != nil || relationships != 1 {
		t.Fatal("stale relationship fixture was not retained", relationships, err)
	}
	if err := s.Read.QueryRowContext(t.Context(), "SELECT count(*) FROM tenant_model_provider WHERE tenant_id = ?", "model-other").Scan(&foreign); err != nil || foreign != 1 {
		t.Fatal("foreign tenant model state changed", foreign, err)
	}
	if err := s.DB.Model(&entity.Tenant{}).Where("id = ?", "model-other").Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	if code, err := svc.AddModelProvider("OpenAI", "model-other"); err == nil || code != common.CodeNotFound {
		t.Fatal("inactive tenant enrolled provider", code, err)
	}
	if code, err := svc.CreateProviderInstance("DeepSeek", "inactive", "controlled-inactive-credential", "", "default", "model-other"); err == nil || code != common.CodeNotFound {
		t.Fatal("inactive tenant persisted credentials", code, err)
	}
	var instances int
	if err := s.Read.QueryRowContext(t.Context(), "SELECT count(*) FROM tenant_model_instance").Scan(&instances); err != nil || instances != 0 {
		t.Fatal("inactive tenant write survived SQL readback", instances, err)
	}
}

func TestCanonicalModelDefaultSlotTypeVocabularySQL(t *testing.T) {
	s := canonicalModelDatabase(t)
	svc := NewModelProviderService()
	if code, err := svc.AddModelProvider("OpenAI", "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if code, err := svc.CreateProviderInstance("OpenAI", "default", "controlled-slot-key", "http://controlled-slot.invalid", "default", "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	request := &AddModelRequest{ProviderName: "OpenAI", InstanceName: "default", Models: []ModelRequest{
		{ModelName: "whisper-1", ModelTypes: []string{"asr"}},
		{ModelName: "gpt-4o", ModelTypes: []string{"vision"}},
	}}
	if code, err := svc.AddModel(request, "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if err := s.DB.Model(&entity.Tenant{}).Where("id = ?", "model-owner").Updates(map[string]any{"asr_id": "whisper-1@default@OpenAI", "img2txt_id": "gpt-4o@default@OpenAI"}).Error; err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		slot       entity.ModelType
		name, kind string
	}{{entity.ModelTypeSpeech2Text, "whisper-1", "asr"}, {entity.ModelTypeImage2Text, "gpt-4o", "vision"}} {
		driver, name, api, _, err := svc.GetTenantDefaultModelByType("model-owner", test.slot)
		if err != nil || driver == nil || name != test.name || api == nil || api.ApiKey == nil || *api.ApiKey != "controlled-slot-key" || api.BaseURL == nil || *api.BaseURL != "http://controlled-slot.invalid" {
			t.Fatal("default slot did not resolve canonical provider capability", test.slot, name, err)
		}
		var kind string
		if err := s.Read.QueryRowContext(t.Context(), "SELECT model_type FROM tenant_model WHERE model_name = ?", test.name).Scan(&kind); err != nil || kind != test.kind {
			t.Fatal("slot mapping rewrote stored capability", kind, err)
		}
	}
	if err := s.DB.Model(&entity.TenantModel{}).Where("model_name = ?", "whisper-1").Update("status", "inactive").Error; err != nil {
		t.Fatal(err)
	}
	if _, _, _, _, err := svc.GetTenantDefaultModelByType("model-owner", entity.ModelTypeSpeech2Text); err == nil {
		t.Fatal("disabled ASR default resolved through catalog fallback")
	}
}
