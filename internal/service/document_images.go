package service

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	_ "golang.org/x/image/bmp"
	_ "golang.org/x/image/webp"
	"gorm.io/gorm"
	"image"
	"image/gif"
	_ "image/jpeg"
	_ "image/png"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"net/url"
	"strings"
	"unicode"
)

var ErrImageUnavailable = errors.New("image is unavailable")
var ErrInvalidImage = errors.New("invalid image request or bytes")

func validImageID(id string) bool {
	return strings.TrimSpace(id) != "" && len(id) <= 1024 && !strings.ContainsFunc(id, func(r rune) bool { return unicode.IsControl(r) })
}

// GetThumbnails returns SQL-registered thumbnails after fresh active membership checks.
func (s *DocumentService) GetThumbnails(ctx context.Context, userID string, ids []string) (map[string]*string, error) {
	if len(ids) == 0 || len(ids) > 100 {
		return nil, ErrInvalidImage
	}
	for _, id := range ids {
		if !validImageID(id) {
			return nil, ErrInvalidImage
		}
	}
	var documents []entity.Document
	if err := s.db.WithContext(ctx).Select("id", "kb_id", "thumbnail").Where("id IN ?", ids).Find(&documents).Error; err != nil {
		return nil, err
	}
	result := map[string]*string{}
	access := map[string]bool{}
	for _, doc := range documents {
		allowed, known := access[doc.KbID]
		if !known {
			_, err := s.kbDAO.GetReadableByID(ctx, doc.KbID, userID)
			if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
				return nil, err
			}
			allowed = err == nil
			access[doc.KbID] = allowed
		}
		if !allowed {
			continue
		}
		value := doc.Thumbnail
		if value != nil && *value != "" && !strings.HasPrefix(*value, "data:image/") {
			encoded := "/api/v1/documents/images/" + doc.KbID + "-" + url.PathEscape(*value)
			value = &encoded
		}
		result[doc.ID] = value
	}
	return result, nil
}

// GetDocumentImage authorizes exact SQL/index registration before the object read.
func (s *DocumentService) GetDocumentImage(ctx context.Context, userID, imageID string) ([]byte, string, error) {
	namespace, key, found := strings.Cut(imageID, "-")
	if !found || !validImageID(imageID) || !validImageID(namespace) || !validImageID(key) {
		return nil, "", ErrInvalidImage
	}
	kb, err := s.kbDAO.GetReadableByID(ctx, namespace, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, "", ErrImageUnavailable
	}
	if err != nil {
		return nil, "", err
	}
	var count int64
	if err = s.db.WithContext(ctx).Model(&entity.Document{}).Where("kb_id = ? AND thumbnail = ?", namespace, key).Count(&count).Error; err != nil {
		return nil, "", err
	}
	if count == 0 {
		if s.docEngine == nil {
			return nil, "", fmt.Errorf("image index is unavailable")
		}
		result, err := s.docEngine.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_" + kb.TenantID}, KbIDs: []string{kb.ID}, Filter: map[string]interface{}{"img_id": imageID, "kb_id": kb.ID}, SelectFields: []string{"img_id", "doc_id", "kb_id"}, Limit: 100, IncludeUnavailable: true})
		if err != nil {
			return nil, "", err
		}
		if result == nil {
			return nil, "", fmt.Errorf("invalid image index result")
		}
		ids := []string{}
		for _, row := range result.Chunks {
			docID, ok := row["doc_id"].(string)
			scopeOK := row["kb_id"] == kb.ID
			if many, ok := row["kb_id"].([]interface{}); ok {
				for _, v := range many {
					if v == kb.ID {
						scopeOK = true
					}
				}
			}
			if many, ok := row["kb_id"].([]string); ok {
				for _, v := range many {
					if v == kb.ID {
						scopeOK = true
					}
				}
			}
			if ok && scopeOK && row["img_id"] == imageID {
				ids = append(ids, docID)
			}
		}
		if len(ids) == 0 {
			return nil, "", ErrImageUnavailable
		}
		if err = s.db.WithContext(ctx).Model(&entity.Document{}).Where("kb_id = ? AND id IN ?", kb.ID, ids).Count(&count).Error; err != nil {
			return nil, "", err
		}
		if count == 0 {
			return nil, "", ErrImageUnavailable
		}
	}
	store := storage.GetStorageFactory().GetStorage()
	if store == nil {
		return nil, "", fmt.Errorf("image storage is unavailable")
	}
	data, err := store.Get(namespace, key)
	if err != nil {
		return nil, "", err
	}
	mime, err := rasterMIME(data)
	if err != nil {
		return nil, "", err
	}
	return data, mime, nil
}

func rasterMIME(data []byte) (string, error) {
	if len(data) == 0 || len(data) > 32<<20 {
		return "", ErrInvalidImage
	}
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || cfg.Width < 1 || cfg.Height < 1 || int64(cfg.Width)*int64(cfg.Height) > 40_000_000 {
		return "", ErrInvalidImage
	}
	mime := map[string]string{"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}[format]
	if mime == "" {
		return "", ErrInvalidImage
	}
	if format == "gif" {
		if !boundedGIF(data) {
			return "", ErrInvalidImage
		}
		decoded, err := gif.DecodeAll(bytes.NewReader(data))
		if err != nil {
			return "", ErrInvalidImage
		}
		var pixels int64
		for _, frame := range decoded.Image {
			pixels += int64(frame.Bounds().Dx()) * int64(frame.Bounds().Dy())
			if pixels > 40_000_000 {
				return "", ErrInvalidImage
			}
		}
	} else if _, _, err = image.Decode(bytes.NewReader(data)); err != nil {
		return "", ErrInvalidImage
	}
	return mime, nil
}

// boundedGIF scans frame descriptors before DecodeAll can allocate frame pixels.
func boundedGIF(data []byte) bool {
	if len(data) < 13 {
		return false
	}
	offset := 13
	if data[10]&0x80 != 0 {
		offset += 3 * (1 << ((data[10] & 7) + 1))
	}
	pixels, frames := int64(0), 0
	skipBlocks := func() bool {
		for {
			if offset >= len(data) {
				return false
			}
			n := int(data[offset])
			offset++
			if n == 0 {
				return true
			}
			if n > len(data)-offset {
				return false
			}
			offset += n
		}
	}
	for offset < len(data) {
		marker := data[offset]
		offset++
		switch marker {
		case 0x3b:
			return frames > 0
		case 0x21:
			if offset >= len(data) {
				return false
			}
			offset++
			if !skipBlocks() {
				return false
			}
		case 0x2c:
			if len(data)-offset < 9 {
				return false
			}
			width := int(data[offset+4]) | int(data[offset+5])<<8
			height := int(data[offset+6]) | int(data[offset+7])<<8
			pixels += int64(width) * int64(height)
			frames++
			if pixels > 40_000_000 || frames > 1000 {
				return false
			}
			packed := data[offset+8]
			offset += 9
			if packed&0x80 != 0 {
				offset += 3 * (1 << ((packed & 7) + 1))
			}
			if offset >= len(data) {
				return false
			}
			offset++
			if !skipBlocks() {
				return false
			}
		default:
			return false
		}
	}
	return false
}
