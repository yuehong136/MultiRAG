package kg

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"multirag/internal/common"
	"multirag/internal/engine/types"
	"multirag/internal/entity/models"
)

func isCancellation(err error) bool {
	return errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)
}

func rewrite(ctx context.Context, model *models.ChatModel, question string, pool map[string][]string) ([]string, []string, error) {
	if err := ctx.Err(); err != nil {
		return nil, nil, err
	}
	if model == nil {
		return nil, []string{question}, nil
	}
	if model.ModelDriver == nil || model.ModelName == nil {
		return nil, nil, fmt.Errorf("KG chat model unavailable")
	}
	if pool == nil {
		pool = map[string][]string{}
	}
	raw, err := json.Marshal(pool)
	if err != nil {
		return nil, nil, err
	}
	local := *model
	api := models.APIConfig{}
	if model.APIConfig != nil {
		api = *model.APIConfig
	}
	api.Context = ctx
	local.APIConfig = &api
	response, err := local.ChatWithMessages([]models.Message{{Role: "system", Content: common.BuildQueryRewritePrompt(question, string(raw))}, {Role: "user", Content: "Output:"}}, nil)
	if err != nil {
		return nil, nil, fmt.Errorf("KG rewrite failed: %w", err)
	}
	parsed, err := common.ParseQueryRewriteResponse(*response.Answer)
	if err != nil {
		return nil, nil, fmt.Errorf("KG rewrite failed: %w", err)
	}
	return parsed.TypeKeywords, parsed.Entities, nil
}

func (p *Pipeline) query(ctx context.Context, scopes []kgScope, dense *types.MatchDenseExpr, terms string, relation bool) ([]map[string]interface{}, error) {
	if strings.TrimSpace(terms) == "" {
		return nil, nil
	}
	return p.search.search(ctx, scopes, func(ids []string) *types.SearchRequest {
		var req *types.SearchRequest
		if relation {
			req = buildRelationSearchRequest(ids, terms, nil, 50)
			req.MatchExprs[0].(*types.MatchTextExpr).Fields = []string{"content_ltks", "from_entity_kwd", "to_entity_kwd"}
		} else {
			req = buildEntitySearchRequest(ids, terms, nil, 50)
		}
		if dense != nil {
			text := req.MatchExprs[0].(*types.MatchTextExpr)
			localDense := *dense
			localDense.EmbeddingData = append([]float64(nil), dense.EmbeddingData...)
			localDense.ExtraOptions = map[string]interface{}{}
			for key, value := range dense.ExtraOptions {
				localDense.ExtraOptions[key] = value
			}
			// The complete pipeline uses equal text/vector fusion; preparatory search
			// retains its established 0.05/0.95 contract.
			req.MatchExprs = []interface{}{text, &localDense, &types.FusionExpr{Method: "weighted_sum", TopN: 50, FusionParams: map[string]interface{}{"weights": "0.5,0.5"}}}
		}
		return req
	})
}

func parseNeighbors(raw string) []NhopEntity {
	var rows []NhopEntity
	if raw == "" || json.Unmarshal([]byte(raw), &rows) != nil {
		return nil
	}
	result := make([]NhopEntity, 0, len(rows))
	for _, row := range rows {
		valid := len(row.Path) > 1 && len(row.Weights) == len(row.Path)-1
		for _, name := range row.Path {
			valid = valid && strings.TrimSpace(name) != ""
		}
		for _, weight := range row.Weights {
			valid = valid && finiteKGScore(weight)
		}
		if valid {
			result = append(result, row)
		}
	}
	return result
}

func communityContent(reports []KGCommunityReport, budget int) string {
	sort.SliceStable(reports, func(i, j int) bool {
		if reports[i].Weight != reports[j].Weight {
			return reports[i].Weight > reports[j].Weight
		}
		return reports[i].Title < reports[j].Title
	})
	var result strings.Builder
	for _, row := range reports {
		content, evidence := row.Content, ""
		var parsed map[string]json.RawMessage
		if json.Unmarshal([]byte(row.Content), &parsed) == nil && parsed != nil {
			_ = json.Unmarshal(parsed["report"], &content)
			_ = json.Unmarshal(parsed["evidences"], &evidence)
		}
		section := fmt.Sprintf("\n# %d. %s\n## Content\n%s\n## Evidences\n%s\n", 1, row.Title, content, evidence)
		// Budget the concatenation to avoid cumulative rounding differences.
		if NumTokensFromString(result.String()+section) > budget {
			break
		}
		result.WriteString(section)
	}
	return result.String()
}

// Indexed relations are undirected canonical pairs. Preserve the low-level
// directed path primitive, then combine reverse contributions at this boundary.
func canonicalEdge(edge Edge) Edge {
	if edge.From > edge.To {
		return Edge{edge.To, edge.From}
	}
	return edge
}

func canonicalPaths(paths map[Edge]EdgeScore) map[Edge]EdgeScore {
	edges := make([]Edge, 0, len(paths))
	for edge := range paths {
		edges = append(edges, edge)
	}
	sort.Slice(edges, func(i, j int) bool {
		if edges[i].From != edges[j].From {
			return edges[i].From < edges[j].From
		}
		return edges[i].To < edges[j].To
	})
	result := make(map[Edge]EdgeScore, len(paths))
	for _, edge := range edges {
		value := paths[edge]
		canonical := canonicalEdge(edge)
		prior, exists := result[canonical]
		if !exists {
			result[canonical] = value
			continue
		}
		if sum := prior.Sim + value.Sim; finiteKGScore(sum) {
			prior.Sim = sum
		}
		prior.PageRank = max(prior.PageRank, value.PageRank)
		result[canonical] = prior
	}
	return result
}
