package types

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"time"
)

var ErrMemoryWriteUnavailable = errors.New("memory message writes are unavailable for this Go store")
var memoryScopeID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// MemoryMessageStore is a separate capability. Chunk writers enforce kb_id and
// cannot safely update the Python message schema, which uses memory_id instead.
type MemoryMessageStore interface {
	ForgetMemoryMessage(context.Context, string, string, int64, time.Time) (bool, error)
}

func MemoryMessageAddress(tenantID, memoryID string, messageID int64) (string, string, error) {
	if !memoryScopeID.MatchString(tenantID) || !memoryScopeID.MatchString(memoryID) || messageID < 0 {
		return "", "", fmt.Errorf("invalid memory message scope")
	}
	return "memory_" + tenantID, fmt.Sprintf("%s_%d", memoryID, messageID), nil
}
