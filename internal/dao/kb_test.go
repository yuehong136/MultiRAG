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

package dao

import (
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

// setupKBTestDB initializes an in-memory SQLite database for KB DAO tests.
func setupKBTestDB(t *testing.T) *gorm.DB {
	t.Helper()

	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{
		TranslateError: true,
	})
	if err != nil {
		t.Fatalf("failed to open sqlite: %v", err)
	}

	// Migrate knowledgebase table
	if err := db.AutoMigrate(&entity.Knowledgebase{}); err != nil {
		t.Fatalf("failed to migrate: %v", err)
	}

	return db
}

func testKnowledgebase(t *testing.T, db *gorm.DB, id string, docNum, tokenNum, chunkNum int64) *entity.Knowledgebase {
	t.Helper()
	kb := &entity.Knowledgebase{
		ID:       id,
		TenantID: "tenant-1",
		Name:     "test-kb-" + id,
		EmbdID:   "embd-1",
		DocNum:   docNum,
		TokenNum: tokenNum,
		ChunkNum: chunkNum,
		Status:   stringPtr(string(entity.StatusValid)),
	}
	if err := db.Create(kb).Error; err != nil {
		t.Fatalf("failed to create test kb: %v", err)
	}
	return kb
}

func stringPtr(s string) *string {
	return &s
}

func TestKnowledgebaseDAO_DecreaseDocumentNum(t *testing.T) {
	db := setupKBTestDB(t)
	pushDB(t, db)
	dao := NewKnowledgebaseDAO()

	testKnowledgebase(t, db, "kb-1", 5, 100, 50)

	err := dao.DecreaseDocumentNum("kb-1", 1, 20, 10)
	if err != nil {
		t.Fatalf("DecreaseDocumentNum failed: %v", err)
	}

	kb, err := dao.GetByID("kb-1")
	if err != nil {
		t.Fatalf("GetByID failed: %v", err)
	}
	if kb.DocNum != 4 {
		t.Fatalf("doc_num: expected 4, got %d", kb.DocNum)
	}
	if kb.TokenNum != 90 {
		t.Fatalf("token_num: expected 90, got %d", kb.TokenNum)
	}
	if kb.ChunkNum != 30 {
		t.Fatalf("chunk_num: expected 30, got %d", kb.ChunkNum)
	}
}

func TestKnowledgebaseDAO_DecreaseDocumentNum_ZeroDecrement(t *testing.T) {
	db := setupKBTestDB(t)
	pushDB(t, db)
	dao := NewKnowledgebaseDAO()

	testKnowledgebase(t, db, "kb-2", 3, 60, 15)

	err := dao.DecreaseDocumentNum("kb-2", 0, 0, 0)
	if err != nil {
		t.Fatalf("DecreaseDocumentNum failed: %v", err)
	}

	kb, err := dao.GetByID("kb-2")
	if err != nil {
		t.Fatalf("GetByID failed: %v", err)
	}
	if kb.DocNum != 3 {
		t.Fatalf("doc_num should be unchanged: expected 3, got %d", kb.DocNum)
	}
	if kb.TokenNum != 60 {
		t.Fatalf("token_num should be unchanged: expected 60, got %d", kb.TokenNum)
	}
	if kb.ChunkNum != 15 {
		t.Fatalf("chunk_num should be unchanged: expected 15, got %d", kb.ChunkNum)
	}
}

func TestKnowledgebaseAccessRequiresActiveIdentitySQL(t *testing.T) {
	f := scratch.New(t)
	prior := DB
	DB = f.DB
	t.Cleanup(func() { DB = prior })
	if err := DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	for _, id := range []string{"owner", "member", "foreign"} {
		if err := DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, Status: &active, IsActive: "1"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := DB.Create(&entity.Tenant{ID: "owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := DB.Create(&entity.UserTenant{ID: "relation", TenantID: "owner", UserID: "member", Role: "normal", Status: &active, InvitedBy: "owner"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := DB.Create(&entity.Knowledgebase{ID: "kb", TenantID: "owner", CreatedBy: "owner", Name: "kb", Permission: "team", Status: &active, ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	dao := NewKnowledgebaseDAO()
	if !dao.Accessible("kb", "owner") || !dao.Accessible("kb", "member") || dao.Accessible("kb", "foreign") {
		t.Fatal("canonical owner/team visibility")
	}
	update := func(table, key, field string, value interface{}) {
		t.Helper()
		if err := DB.Table(table).Where("id = ?", key).Update(field, value).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, role := range []string{"invite", "unknown"} {
		update("user_tenant", "relation", "role", role)
		if dao.Accessible("kb", "member") {
			t.Fatal("nonmember role accepted", role)
		}
	}
	for _, role := range []string{"owner", "normal", "admin"} {
		update("user_tenant", "relation", "role", role)
		if !dao.Accessible("kb", "member") {
			t.Fatal("active member rejected", role)
		}
	}
	update("user_tenant", "relation", "status", "0")
	if dao.Accessible("kb", "member") {
		t.Fatal("revoked membership accepted")
	}
	update("user_tenant", "relation", "status", "1")
	update("user", "member", "is_active", "0")
	if dao.Accessible("kb", "member") {
		t.Fatal("disabled actor accepted")
	}
	update("user", "member", "is_active", "1")
	update("user", "owner", "status", "0")
	if dao.Accessible("kb", "owner") {
		t.Fatal("deleted owner accepted")
	}
	update("user", "owner", "status", "1")
	update("tenant", "owner", "status", "0")
	if dao.Accessible("kb", "owner") || dao.Accessible("kb", "member") {
		t.Fatal("disabled tenant accepted")
	}
	update("tenant", "owner", "status", "1")
	update("knowledgebase", "kb", "permission", "me")
	if !dao.Accessible("kb", "owner") || dao.Accessible("kb", "member") {
		t.Fatal("private visibility changed")
	}
	update("knowledgebase", "kb", "permission", "unknown")
	if dao.Accessible("kb", "member") {
		t.Fatal("unknown permission broadened")
	}
	update("knowledgebase", "kb", "status", "0")
	if dao.Accessible("kb", "owner") {
		t.Fatal("deleted dataset accepted")
	}
	var count int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM user_tenant WHERE tenant_id='owner'").Scan(&count); err != nil || count != 1 {
		t.Fatal("independent membership readback", count, err)
	}
}
