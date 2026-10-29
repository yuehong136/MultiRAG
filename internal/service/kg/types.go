package kg

// KGEntity represents a knowledge graph entity.
type KGEntity struct {
	Name        string  // entity_kwd
	Type        string  // entity_type_kwd
	PageRank    float64 // rank_flt
	Similarity  float64 // _score
	Description string  // content_with_weight
	NhopJSON    string  // n_hop_with_weight
	Sim         float64
	NhopEnts    []NhopEntity
}

// KGRelation represents a relation between two entities.
type KGRelation struct {
	From        string  // from_entity_kwd
	To          string  // to_entity_kwd
	Weight      int     // weight_int
	Description string  // content_with_weight
	Similarity  float64 // engine score
	Sim         float64
	PageRank    float64
}

// KGCommunityReport represents a community report.
type KGCommunityReport struct {
	Title       string   // docnm_kwd
	Content     string   // content_with_weight
	Weight      float64  // weight_flt
	Entities    string   // entities_kwd
	EntityNames []string // complete keyword array
}

// NhopEntity is a neighbor path with one graph weight per edge.
type NhopEntity struct {
	Path    []string
	Weights []float64
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
