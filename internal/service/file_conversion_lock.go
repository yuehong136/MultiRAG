package service

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"sort"
	"time"

	"gorm.io/gorm"
)

// LinkToDatasets keeps a stable SQL session advisory lock across both insertion
// and retirement transactions. A row lock cannot span these phases: retirement
// itself locks the same File, and holding that row here would self-deadlock.
func (s *File2DocumentService) LinkToDatasets(ctx context.Context, actor string, req LinkToDatasetsRequest, mode string) error {
	ids, err := linkIDs(req.FileIDs)
	if err != nil || len(ids) == 0 {
		return ErrLinkArgument
	}
	if _, err = linkIDs(req.KBIDs); err != nil || (mode != "replace" && mode != "add") {
		return ErrLinkArgument
	}
	if actor == "" {
		return ErrLinkUnavailable
	}
	if s == nil || s.db == nil {
		return errors.New("file conversion SQL unavailable")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if s.db.Dialector.Name() != "postgres" {
		return errors.New("file conversion serialization unavailable for this SQL dialect")
	}
	return s.db.WithContext(ctx).Connection(func(session *gorm.DB) (resultErr error) {
		// Connection callbacks expose a reusable statement. Query builders must
		// clone it, rather than carrying a File model/WHERE into actor queries.
		session = session.Session(&gorm.Session{NewDB: true, Context: ctx})
		_, leaves, err := collectLinkFiles(session, actor, ids)
		if err != nil {
			return err
		}
		locks, err := linkIDs(append(append([]string{}, ids...), leaves...))
		if err != nil {
			return err
		}
		sort.Strings(locks)
		held := []string{}
		acknowledged := map[string]bool{}
		defer func() {
			cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer stop()
			for i := len(held) - 1; i >= 0; i-- {
				var unlocked bool
				if err := session.WithContext(cleanup).Raw("SELECT pg_advisory_unlock(hashtextextended(?,0))", "multirag:file-link:"+held[i]).Scan(&unlocked).Error; err != nil || (!unlocked && acknowledged[held[i]]) {
					resultErr = errors.Join(resultErr, errors.New("file conversion lock cleanup could not be confirmed"))
					// Never return a session with uncertain advisory locks to the
					// pool. Discarding its physical connection releases those locks.
					if connection, ok := session.Statement.ConnPool.(*sql.Conn); ok {
						_ = connection.Raw(func(any) error { return driver.ErrBadConn })
					}
					break
				}
			}
		}()
		for _, id := range locks {
			held = append(held, id)
			if err = session.Exec("SELECT pg_advisory_lock(hashtextextended(?,0))", "multirag:file-link:"+id).Error; err != nil {
				return err
			}
			acknowledged[id] = true
		}
		owned := *s
		owned.db = session
		if deletion, ok := s.deletion.(*DocumentDeleteService); ok {
			copy := *deletion
			copy.db = session
			owned.deletion = &copy
		}
		return owned.linkToDatasets(ctx, actor, req, mode, leaves)
	})
}
