package skills

import (
	"context"
	"time"

	"github.com/gin-gonic/gin"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"multirag/internal/dao"
	"multirag/internal/server"
)

// Attach adds native Skills routes without changing or migrating legacy services.
func Attach(engine *gin.Engine, cfg *server.Config) func() {
	ctx, cancel := context.WithCancel(context.Background())
	var blobs BlobStore
	if cfg.StorageEngine.Type == server.StorageMinio {
		if store, e := NewObjectStore(cfg.StorageEngine.Minio); e == nil {
			blobs = store
		}
	}
	var index IndexStore
	var milvus *MilvusStore
	if cfg.DocEngine.Type == server.EngineMilvus && cfg.DocEngine.Milvus != nil {
		m := cfg.DocEngine.Milvus
		connect, done := context.WithTimeout(ctx, 10*time.Second)
		store, e := NewMilvus(connect, &mc.ClientConfig{Address: m.Hosts, Username: m.Username, Password: m.Password, APIKey: m.Token, DBName: m.DBName})
		done()
		if e == nil {
			index = store
			milvus = store
		}
	}
	s := New(dao.DB, blobs, index, &LegacyModels{DB: dao.DB, Providers: dao.GetModelProviderManager()})
	s.Register(engine, func() string { return server.GetVariables().SecretKey })
	finished := make(chan struct{})
	go func() { defer close(finished); s.Run(ctx) }()
	return func() {
		cancel()
		select {
		case <-finished:
		case <-time.After(5 * time.Second):
		}
		if milvus != nil {
			milvus.Client.Close(context.Background())
		}
	}
}
