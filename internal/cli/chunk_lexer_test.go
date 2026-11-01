package cli

import "testing"

func TestChunkLexerEndOfInput(t *testing.T) {
	for _, input := range []string{"CHUNK", "chunk", "CHUNK ", "CHUNK\t\r\n"} {
		lexer := NewLexer(input)
		if token := lexer.NextToken(); token.Type != TokenChunk {
			t.Fatalf("%q: got %#v, want CHUNK", input, token)
		}
		if token := lexer.NextToken(); token.Type != TokenEOF {
			t.Fatalf("%q: got %#v, want EOF", input, token)
		}
	}
	for _, input := range []string{"CHUNK STORE", "CHUNK\tSTORE", "chunk store ", "CHUNK \nSTORE;"} {
		lexer := NewLexer(input)
		if token := lexer.NextToken(); token.Type != TokenChunkStore || token.Value != "CHUNK STORE" {
			t.Fatalf("%q: compound keyword changed: %#v", input, token)
		}
		token := lexer.NextToken()
		if token.Type == TokenSemicolon {
			token = lexer.NextToken()
		}
		if token.Type != TokenEOF {
			t.Fatalf("%q: got %#v, want EOF", input, token)
		}
	}
}
