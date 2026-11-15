package service

import (
	"multirag/internal/entity"
	"testing"
)

func TestAfter67SearchScopeSQL(t *testing.T) {
	f := canonicalLink(t)
	if err := f.DB.AutoMigrate(&entity.Search{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	if err := f.DB.Create(&entity.UserTenant{ID: f.owner, UserID: f.owner, TenantID: f.owner, Role: "owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	id := f.Prefix + "search"
	if err := f.DB.Create(&entity.Search{ID: id, TenantID: f.owner, CreatedBy: f.owner, Name: "Saved", SearchConfig: entity.JSONMap{"chat_id": "model@instance@provider", "temperature": 0}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	s := NewSearchService()
	for _, actor := range []string{f.owner, f.member} {
		record, err := s.GetSearchDetail(actor, id)
		if err != nil || record.ID != id || record.SearchConfig["chat_id"] != "model@instance@provider" {
			t.Fatal("authorized saved search", actor, record, err)
		}
	}
	for _, actor := range []string{f.other, f.invite, "missing"} {
		if record, err := s.GetSearchDetail(actor, id); err == nil || record != nil {
			t.Fatal("unauthorized saved search", actor, record, err)
		}
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", f.owner).Update("is_active", "0").Error; err != nil {
		t.Fatal(err)
	}
	if record, err := s.GetSearchDetail(f.owner, id); err == nil || record != nil {
		t.Fatal("inactive actor accessed search")
	}
}
