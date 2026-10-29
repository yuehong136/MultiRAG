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
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

// QueryRewriteResult contains answer types and concrete terms from a query.
type QueryRewriteResult struct {
	TypeKeywords []string `json:"answer_type_keywords"`
	Entities     []string `json:"entities_from_query"`
}

// queryRewritePromptTmpl follows the GraphRAG minirag_query2kwd prompt.
const queryRewritePromptTmpl = `---Role---

You are a helpful assistant tasked with identifying both answer-type and low-level keywords in the user's query.

---Goal---

Given the query, list both answer-type and low-level keywords.
answer_type_keywords focus on the type of the answer to the certain query, while low-level keywords focus on specific entities, details, or concrete terms.
The answer_type_keywords must be selected from Answer type pool.
This pool is in the form of a dictionary, where the key represents the Type you should choose from and the value represents the example samples.

---Instructions---

- Output the keywords in JSON format.
- The JSON should have two keys:
  - "answer_type_keywords" for the types of the answer. In this list, the types with the highest likelihood should be placed at the forefront. No more than 3.
  - "entities_from_query" for specific entities or details. It must be extracted from the query.
######################
-Examples-
######################
Example 1:

Query: "How does international trade influence global economic stability?"
Answer type pool: {
 'PERSONAL LIFE': ['FAMILY TIME', 'HOME MAINTENANCE'],
 'STRATEGY': ['MARKETING PLAN', 'BUSINESS EXPANSION'],
 'SERVICE FACILITATION': ['ONLINE SUPPORT', 'CUSTOMER SERVICE TRAINING'],
 'PERSON': ['JANE DOE', 'JOHN SMITH'],
 'FOOD': ['PASTA', 'SUSHI'],
 'EMOTION': ['HAPPINESS', 'ANGER'],
 'PERSONAL EXPERIENCE': ['TRAVEL ABROAD', 'STUDYING ABROAD'],
 'INTERACTION': ['TEAM MEETING', 'NETWORKING EVENT'],
 'BEVERAGE': ['COFFEE', 'TEA'],
 'PLAN': ['ANNUAL BUDGET', 'PROJECT TIMELINE'],
 'GEO': ['NEW YORK CITY', 'SOUTH AFRICA'],
 'GEAR': ['CAMPING TENT', 'CYCLING HELMET'],
 'EMOJI': ['🎉', '🚀'],
 'BEHAVIOR': ['POSITIVE FEEDBACK', 'NEGATIVE CRITICISM'],
 'TONE': ['FORMAL', 'INFORMAL'],
 'LOCATION': ['DOWNTOWN', 'SUBURBS']
}
################
Output:
{
  "answer_type_keywords": ["STRATEGY","PERSONAL LIFE"],
  "entities_from_query": ["Trade agreements", "Tariffs", "Currency exchange", "Imports", "Exports"]
}
#############################
Example 2:

Query: "Where is the capital of the United States?"
Answer type pool: {
 'ORGANIZATION': ['GREENPEACE', 'RED CROSS'],
 'PERSONAL LIFE': ['DAILY WORKOUT', 'HOME COOKING'],
 'STRATEGY': ['FINANCIAL INVESTMENT', 'BUSINESS EXPANSION'],
 'SERVICE FACILITATION': ['ONLINE SUPPORT', 'CUSTOMER SERVICE TRAINING'],
 'PERSON': ['ALBERTA SMITH', 'BENJAMIN JONES'],
 'FOOD': ['PASTA CARBONARA', 'SUSHI PLATTER'],
 'EMOTION': ['HAPPINESS', 'SADNESS'],
 'PERSONAL EXPERIENCE': ['TRAVEL ADVENTURE', 'BOOK CLUB'],
 'INTERACTION': ['TEAM BUILDING', 'NETWORKING MEETUP'],
 'BEVERAGE': ['LATTE', 'GREEN TEA'],
 'PLAN': ['WEIGHT LOSS', 'CAREER DEVELOPMENT'],
 'GEO': ['PARIS', 'NEW YORK'],
 'GEAR': ['CAMERA', 'HEADPHONES'],
 'EMOJI': ['🏢', '🌍'],
 'BEHAVIOR': ['POSITIVE THINKING', 'STRESS MANAGEMENT'],
 'TONE': ['FRIENDLY', 'PROFESSIONAL'],
 'LOCATION': ['DOWNTOWN', 'SUBURBS']
}
################
Output:
{
  "answer_type_keywords": ["LOCATION"],
  "entities_from_query": ["capital of the United States", "Washington", "New York"]
}
#############################

-Real Data-
######################
Query: {query}
Answer type pool:{TYPE_POOL}
######################
Output:
`

// BuildQueryRewritePrompt substitutes each template placeholder once. Braces or
// placeholder-looking text inside either caller value are kept verbatim.
func BuildQueryRewritePrompt(question string, typePoolJSON string) string {
	return strings.NewReplacer("{query}", question, "{TYPE_POOL}", typePoolJSON).Replace(queryRewritePromptTmpl)
}

// ParseQueryRewriteResponse accepts a JSON object, optionally inside Markdown or
// surrounding prose. It rejects multiple objects and invalid shapes instead of
// treating malformed model output as successful empty keyword extraction.
func ParseQueryRewriteResponse(response string) (*QueryRewriteResult, error) {
	response = strings.TrimSpace(response)
	if json.Valid([]byte(response)) {
		return parseQueryRewriteObject(response)
	}
	start, end, depth := -1, -1, 0
	inString, escaped := false, false
	for i := 0; i < len(response); i++ {
		char := response[i]
		if depth == 0 {
			if char == '{' {
				if start != -1 {
					return nil, fmt.Errorf("query rewrite response contains multiple JSON objects")
				}
				start, depth = i, 1
			}
			continue
		}
		if inString {
			if escaped {
				escaped = false
			} else if char == '\\' {
				escaped = true
			} else if char == '"' {
				inString = false
			}
			continue
		}
		switch char {
		case '"':
			inString = true
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				end = i + 1
			}
		}
	}
	if start < 0 || end < 0 || depth != 0 {
		return nil, fmt.Errorf("query rewrite response contains no complete JSON object")
	}
	return parseQueryRewriteObject(response[start:end])
}

func parseQueryRewriteObject(data string) (*QueryRewriteResult, error) {
	decoder := json.NewDecoder(strings.NewReader(data))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, fmt.Errorf("query rewrite response must be a JSON object")
	}
	fields := make(map[string][]string, 2)
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return nil, fmt.Errorf("invalid query rewrite object: %w", err)
		}
		key, ok := keyToken.(string)
		if !ok || (key != "answer_type_keywords" && key != "entities_from_query") {
			return nil, fmt.Errorf("unknown query rewrite field")
		}
		if _, duplicate := fields[key]; duplicate {
			return nil, fmt.Errorf("duplicate query rewrite field %s", key)
		}
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			return nil, fmt.Errorf("invalid query rewrite field %s: %w", key, err)
		}
		var values []json.RawMessage
		if err := json.Unmarshal(raw, &values); err != nil || values == nil {
			return nil, fmt.Errorf("query rewrite field %s must be a nonnull string array", key)
		}
		keywords := make([]string, 0, len(values))
		for _, value := range values {
			var text string
			if len(value) == 0 || value[0] != '"' || json.Unmarshal(value, &text) != nil {
				return nil, fmt.Errorf("query rewrite field %s must contain strings", key)
			}
			if strings.TrimSpace(text) == "" {
				return nil, fmt.Errorf("query rewrite field %s contains an empty keyword", key)
			}
			keywords = append(keywords, text)
		}
		fields[key] = keywords
	}
	if _, err := decoder.Token(); err != nil {
		return nil, fmt.Errorf("invalid query rewrite object: %w", err)
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, fmt.Errorf("unexpected trailing query rewrite data")
	}
	types, typesPresent := fields["answer_type_keywords"]
	entities, entitiesPresent := fields["entities_from_query"]
	if !typesPresent || !entitiesPresent {
		return nil, fmt.Errorf("query rewrite response requires both keyword arrays")
	}
	return &QueryRewriteResult{TypeKeywords: types[:min(3, len(types))], Entities: entities[:min(5, len(entities))]}, nil
}
