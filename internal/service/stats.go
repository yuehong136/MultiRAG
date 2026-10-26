package service

import (
	"context"
	"errors"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"time"
)

type StatPoint [2]any
type StatsResponse struct {
	PV      []StatPoint `json:"pv"`
	UV      []StatPoint `json:"uv"`
	Speed   []StatPoint `json:"speed"`
	Tokens  []StatPoint `json:"tokens"`
	Round   []StatPoint `json:"round"`
	ThumbUp []StatPoint `json:"thumb_up"`
}
type conversationStatsRow struct {
	Day      time.Time
	PV       int64
	UV       int64
	Tokens   float64
	Duration float64
	Round    float64
	ThumbUp  int64
}

// GetStats scopes aggregates to the owned tenant or one readable agent.
func (s *SystemService) GetStats(ctx context.Context, userID string, from, to time.Time, canvasID *string) (*StatsResponse, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	if !from.Before(to) {
		return nil, common.CodeArgumentError, errors.New("invalid date range")
	}
	query := dao.DB.WithContext(ctx).Table("t_ai_api4conversations AS a")
	if canvasID != nil {
		var count int64
		if *canvasID == "" {
			return nil, common.CodeArgumentError, errors.New("canvas_id is required")
		}
		owners, err := dao.NewUserTenantDAO().GetTenantIDsByUserID(userID)
		if err != nil {
			return nil, common.CodeServerError, errors.New("Unable to read agents.")
		}
		if err := dao.DB.WithContext(ctx).Model(&entity.UserCanvas{}).Where("id = ? AND user_id IN ? AND (user_id = ? OR permission = ?)", *canvasID, owners, userID, "team").Count(&count).Error; err != nil {
			return nil, common.CodeServerError, errors.New("Unable to read agents.")
		}
		if count != 1 {
			return nil, common.CodeAuthenticationError, errors.New("No authorization.")
		}
		query = query.Where("a.source = ? AND a.dialog_id = ?", "agent", *canvasID)
	} else {
		query = query.Joins("JOIN t_ai_dialogs AS d ON d.id = a.dialog_id").Where("d.tenant_id = ? AND a.source IS NULL", userID)
	}
	var rows []conversationStatsRow
	dayExpression := "date_trunc('day', a.create_date AT TIME ZONE 'UTC')"
	if dao.DB.Dialector.Name() == "mysql" {
		dayExpression = "CAST(DATE(a.create_date) AS DATETIME)"
	}
	err := query.Select(dayExpression+" AS day, count(a.id) AS pv, count(DISTINCT a.user_id) AS uv, COALESCE(sum(a.tokens),0) AS tokens, COALESCE(sum(a.duration),0) AS duration, COALESCE(avg(a.round),0) AS round, COALESCE(sum(a.thumb_up),0) AS thumb_up").Where("a.create_date >= ? AND a.create_date < ?", from, to).Group("day").Order("day ASC").Scan(&rows).Error
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to read statistics.")
	}
	result := &StatsResponse{PV: []StatPoint{}, UV: []StatPoint{}, Speed: []StatPoint{}, Tokens: []StatPoint{}, Round: []StatPoint{}, ThumbUp: []StatPoint{}}
	for _, row := range rows {
		day := row.Day.Format("2006-01-02 00:00:00")
		speed := 0.0
		if row.Duration > 0 {
			speed = row.Tokens / row.Duration
		}
		result.PV = append(result.PV, StatPoint{day, row.PV})
		result.UV = append(result.UV, StatPoint{day, row.UV})
		result.Speed = append(result.Speed, StatPoint{day, speed})
		result.Tokens = append(result.Tokens, StatPoint{day, row.Tokens / 1000})
		result.Round = append(result.Round, StatPoint{day, row.Round})
		result.ThumbUp = append(result.ThumbUp, StatPoint{day, row.ThumbUp})
	}
	return result, common.CodeSuccess, nil
}
