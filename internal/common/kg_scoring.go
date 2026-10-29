// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package common

import (
	"encoding/csv"
	"encoding/json"
	"math"
	"sort"
	"strconv"
	"strings"
)

// KGEntity is an entity and its query, graph and neighbor scores.
type KGEntity struct {
	Sim         float64
	PageRank    float64
	Description string
	NhopEnts    []NhopEntity
}

// NhopEntity is a neighbor path with one graph weight per edge.
type NhopEntity struct {
	Path    []string
	Weights []float64
}

// KGRelation is a relation and its query and graph scores.
type KGRelation struct {
	Sim         float64
	PageRank    float64
	Description string
}

// Edge is a directed entity pair.
type Edge struct{ From, To string }

// EdgeScore is the accumulated N-hop similarity and graph weight.
type EdgeScore struct{ Sim, PageRank float64 }

// ScoredEntity is a ranked entity ready for output.
type ScoredEntity struct {
	Entity      string
	Score       float64
	Description string
}

// ScoredRelation is a ranked relation ready for output.
type ScoredRelation struct {
	From, To    string
	Score       float64
	Description string
}

func finiteKGScore(value float64) bool { return !math.IsNaN(value) && !math.IsInf(value, 0) }

// AnalyzeNHopPaths sums distance-decayed query similarity. Sorted contributors
// make floating-point addition reproducible; repeated edge weights use their
// maximum, rather than whichever map entry happens to be visited last.
func AnalyzeNHopPaths(entities map[string]*KGEntity) map[Edge]EdgeScore {
	result := make(map[Edge]EdgeScore)
	weightSeen := make(map[Edge]bool)
	names := make([]string, 0, len(entities))
	for name := range entities {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		entity := entities[name]
		if entity == nil || !finiteKGScore(entity.Sim) {
			continue
		}
		for _, neighbor := range entity.NhopEnts {
			for i := 0; i+1 < len(neighbor.Path); i++ {
				edge := Edge{neighbor.Path[i], neighbor.Path[i+1]}
				if edge.From == "" || edge.To == "" {
					continue
				}
				value := result[edge]
				similarity := value.Sim + entity.Sim/(2+float64(i))
				if !finiteKGScore(similarity) {
					continue
				}
				value.Sim = similarity
				if i < len(neighbor.Weights) && finiteKGScore(neighbor.Weights[i]) {
					if !weightSeen[edge] || neighbor.Weights[i] > value.PageRank {
						value.PageRank = neighbor.Weights[i]
					}
					weightSeen[edge] = true
				}
				result[edge] = value
			}
		}
	}
	return result
}

// DoubleHitBoost returns a deep copy with doubled similarity for type hits.
// Invalid and overflowing scores are omitted. Neither input is modified.
func DoubleHitBoost(entities map[string]*KGEntity, types map[string]struct{}) map[string]*KGEntity {
	result := make(map[string]*KGEntity, len(entities))
	for name, entity := range entities {
		if entity == nil || !finiteKGScore(entity.Sim) || !finiteKGScore(entity.PageRank) {
			continue
		}
		copy := *entity
		if _, hit := types[name]; hit {
			copy.Sim *= 2
		}
		if !finiteKGScore(copy.Sim) {
			continue
		}
		copy.NhopEnts = make([]NhopEntity, len(entity.NhopEnts))
		for i, neighbor := range entity.NhopEnts {
			copy.NhopEnts[i] = NhopEntity{append([]string(nil), neighbor.Path...), append([]float64(nil), neighbor.Weights...)}
		}
		result[name] = &copy
	}
	return result
}

// FuseRelationScores returns new relations incorporating N-hop and type boosts.
// Input relations, pointers and N-hop contributions remain caller-owned.
func FuseRelationScores(relations map[Edge]*KGRelation, types map[string]struct{}, paths map[Edge]EdgeScore) map[Edge]*KGRelation {
	result := make(map[Edge]*KGRelation, len(relations)+len(paths))
	for edge, relation := range relations {
		if relation == nil || edge.From == "" || edge.To == "" || !finiteKGScore(relation.Sim) || !finiteKGScore(relation.PageRank) {
			continue
		}
		copy := *relation
		boost := kgTypeBoost(edge, types)
		if path, ok := paths[edge]; ok && finiteKGScore(path.Sim) {
			boost += path.Sim
		}
		copy.Sim *= boost
		if finiteKGScore(copy.Sim) {
			result[edge] = &copy
		}
	}
	for edge, path := range paths {
		if _, exists := relations[edge]; exists {
			continue
		}
		score := path.Sim * kgTypeBoost(edge, types)
		if edge.From != "" && edge.To != "" && finiteKGScore(score) && finiteKGScore(path.PageRank) {
			result[edge] = &KGRelation{Sim: score, PageRank: path.PageRank}
		}
	}
	return result
}

func kgTypeBoost(edge Edge, types map[string]struct{}) float64 {
	boost := 1.0
	if _, ok := types[edge.From]; ok {
		boost++
	}
	if _, ok := types[edge.To]; ok {
		boost++
	}
	return boost
}

// SortAndTrimEntities sorts finite products descending, resolving ties by name.
// A nonpositive limit selects no entries; it never widens the caller's limit.
func SortAndTrimEntities(entities map[string]*KGEntity, topN int) []ScoredEntity {
	result := make([]ScoredEntity, 0)
	if topN <= 0 {
		return result
	}
	for name, entity := range entities {
		if entity != nil && finiteKGScore(entity.Sim) && finiteKGScore(entity.PageRank) && finiteKGScore(entity.Sim*entity.PageRank) {
			result = append(result, ScoredEntity{name, entity.Sim * entity.PageRank, entity.Description})
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Score == result[j].Score {
			return result[i].Entity < result[j].Entity
		}
		return result[i].Score > result[j].Score
	})
	return result[:min(topN, len(result))]
}

// SortAndTrimRelations sorts finite products descending, then by directed pair.
func SortAndTrimRelations(relations map[Edge]*KGRelation, topN int) []ScoredRelation {
	result := make([]ScoredRelation, 0)
	if topN <= 0 {
		return result
	}
	for edge, relation := range relations {
		if relation != nil && finiteKGScore(relation.Sim) && finiteKGScore(relation.PageRank) && finiteKGScore(relation.Sim*relation.PageRank) {
			result = append(result, ScoredRelation{edge.From, edge.To, relation.Sim * relation.PageRank, relation.Description})
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Score == result[j].Score {
			if result[i].From == result[j].From {
				return result[i].To < result[j].To
			}
			return result[i].From < result[j].From
		}
		return result[i].Score > result[j].Score
	})
	return result[:min(topN, len(result))]
}

// NumTokensFromString is a byte-count budget approximation, rounded up at four
// bytes per unit. It is not a model tokenizer and does not enforce model limits.
func NumTokensFromString(value string) int { return len(value)/4 + (len(value)%4+3)/4 }

func kgCSVRecord(fields []string) string {
	var output strings.Builder
	writer := csv.NewWriter(&output)
	_ = writer.Write(fields) // strings.Builder cannot fail a write.
	writer.Flush()
	return output.String()
}

func formatKGCSV(header string, rows [][]string, maxToken int) (string, int) {
	if maxToken <= 0 {
		return "", 0
	}
	var output strings.Builder
	for _, row := range rows {
		headerSize := 0
		if output.Len() == 0 {
			headerSize = len(header)
		}
		record := kgCSVRecord(row)
		size := output.Len() + headerSize + len(record)
		if size/4+(size%4+3)/4 > maxToken {
			break
		}
		if headerSize != 0 {
			output.WriteString(header)
		}
		output.WriteString(record)
	}
	return output.String(), maxToken - NumTokensFromString(output.String())
}

// FormatEntitiesToCSV escapes CSV records and budgets both headers and rows.
func FormatEntitiesToCSV(entities []ScoredEntity, maxToken int) (string, int) {
	rows := make([][]string, 0, len(entities))
	for _, entity := range entities {
		if finiteKGScore(entity.Score) {
			rows = append(rows, []string{entity.Entity, strconv.FormatFloat(entity.Score, 'f', 2, 64), extractDescription(entity.Description)})
		}
	}
	return formatKGCSV("---- Entities ----\nEntity,Score,Description\n", rows, maxToken)
}

// FormatRelationsToCSV escapes CSV records and budgets both headers and rows.
func FormatRelationsToCSV(relations []ScoredRelation, maxToken int) (string, int) {
	rows := make([][]string, 0, len(relations))
	for _, relation := range relations {
		if finiteKGScore(relation.Score) {
			rows = append(rows, []string{relation.From, relation.To, strconv.FormatFloat(relation.Score, 'f', 2, 64), extractDescription(relation.Description)})
		}
	}
	return formatKGCSV("---- Relations ----\nFrom Entity,To Entity,Score,Description\n", rows, maxToken)
}

// BuildKGContent gives entities first use of the shared approximate budget.
func BuildKGContent(entities []ScoredEntity, relations []ScoredRelation, maxToken int) string {
	entityCSV, remaining := FormatEntitiesToCSV(entities, maxToken)
	relationCSV, _ := FormatRelationsToCSV(relations, remaining)
	return entityCSV + relationCSV
}

func extractDescription(description string) string {
	var object map[string]json.RawMessage
	if json.Unmarshal([]byte(description), &object) == nil && object != nil {
		var value string
		_ = json.Unmarshal(object["description"], &value)
		return value
	}
	return description
}
