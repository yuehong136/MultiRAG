package skills

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"multirag/internal/server"
)

func uploadPackage(t *testing.T, name, version, markdown string) *Package {
	t.Helper()
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)
	manifest := UploadManifest{Name: name, Version: version, Files: []FileEntry{{Path: "SKILL.md", Size: int64(len(markdown))}}}
	sum := digestBytes([]byte(markdown))
	manifest.Files[0].SHA256 = sum
	raw, _ := json.Marshal(manifest)
	writer.WriteField("manifest", string(raw))
	file, _ := writer.CreateFormFile("file", "ignored")
	file.Write([]byte(markdown))
	writer.Close()
	pkg, e := parsePackage(multipart.NewReader(&buf, writer.Boundary()))
	if e != nil {
		t.Fatal(e)
	}
	return pkg
}
func digestBytes(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func TestPackageRejectsTraversalAndUnsafeMetadata(t *testing.T) {
	if e := validPath(strings.Repeat("技", 255)); e != nil {
		t.Fatal("valid multibyte segment rejected", e)
	}
	if validPath(strings.Repeat("技", 256)) == nil {
		t.Fatal("oversized path segment accepted")
	}
	for _, path := range []string{"../secret", "/absolute", "a//b", "a/./b", "a\\b", "a\x00b", "e\u0301.txt", "a\u200bb", "C:/absolute"} {
		if validPath(path) == nil {
			t.Errorf("accepted path %q", path)
		}
	}
	for _, text := range []string{"---\nname: orange\ndescription: 123\n---", "---\nname: orange\ndescription: okay\ntags: [1]\n---", "---\nname: orange\ndescription: &text okay\ntags: [*text]\n---", "---\nname: orange\ndescription: okay\n---garbage"} {
		if _, _, e := metadata([]byte(text), "orange"); e == nil {
			t.Errorf("accepted unsafe frontmatter %q", text)
		}
	}
	pkg := uploadPackage(t, "orange", "1.2.3-rc.1+build.2", "---\nname: orange\ndescription: 中文描述\ntags: [fruit]\n---\nText")
	if pkg.Description != "中文描述" || len(pkg.Digest) != 64 || pkg.Manifest.Files[0].Path != "SKILL.md" {
		t.Fatal("invalid canonical package")
	}
}
func TestChunkBoundariesDoNotLoseUTF8(t *testing.T) {
	text := strings.Repeat("技能 x\n", 5000)
	parts, e := splitText(text, 17)
	if e != nil {
		t.Fatal(e)
	}
	if strings.Join(parts, "") != text {
		t.Fatal("text lost at boundaries")
	}
	for _, part := range parts {
		if len(part) > 13 || !utf8.ValidString(part) {
			t.Fatal("invalid chunk")
		}
	}
	if _, e = splitText("技", 1); e == nil {
		t.Fatal("impossible model budget accepted")
	}
	for _, vector := range [][]float64{{math.NaN()}, {0, 0}, {math.SmallestNonzeroFloat64}, make([]float64, 32769)} {
		if _, _, e = vectors32([][]float64{vector}, 1); e == nil {
			t.Fatal("invalid embedding vector accepted")
		}
	}
}
func TestPythonAESCipherCompatibility(t *testing.T) {
	fixtures := map[string]string{"aes-128-cbc": "52414746000102030405060708090a0b0c0d0e0f6172ff9b368d506fb65bcad1f50408ac", "aes-256-cbc": "52414746000102030405060708090a0b0c0d0e0fb67b76aada9fb231d609f80f6f78b983"}
	for algorithm, fixture := range fixtures {
		t.Run(algorithm, func(t *testing.T) {
			t.Setenv("MultiRAG_CRYPTO_ENABLED", "true")
			t.Setenv("MultiRAG_CRYPTO_KEY", "skills-fixture-key")
			t.Setenv("MultiRAG_CRYPTO_ALGORITHM", algorithm)
			s, e := NewObjectStore(&server.MinioConfig{Host: "127.0.0.1:9000"})
			if e != nil {
				t.Fatal(e)
			}
			ciphertext, _ := hex.DecodeString(fixture)
			plain, e := s.decrypt(ciphertext)
			if e != nil || string(plain) != "技能\x00bytes" {
				t.Fatal("Python ciphertext mismatch", e)
			}
			encoded, e := s.encrypt(plain)
			if e != nil {
				t.Fatal(e)
			}
			decoded, e := s.decrypt(encoded)
			if e != nil || !bytes.Equal(decoded, plain) || bytes.Equal(encoded, ciphertext) {
				t.Fatal("roundtrip or random IV failure")
			}
			encoded[len(encoded)-1] ^= 0xff
			if _, e = s.decrypt(encoded); e == nil {
				t.Fatal("corruption accepted")
			}
		})
	}
}
func TestStrictRerankDoesNotInventSuccess(t *testing.T) {
	cases := []struct {
		body  string
		valid bool
	}{{`{"error":"bad"}`, false}, {`{"results":[{"index":0,"relevance_score":0.8}]}`, true}, {`{"data":[{"index":0,"relevance_score":0.7}]}`, true}, {`{"results":[{"relevance_score":0.8}]}`, false}, {`{"results":[{"index":1,"relevance_score":0.8}]}`, false}, {`{"results":[{"index":0}]}`, false}, {`{"results":[]}`, false}}
	for _, test := range cases {
		fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer exact-row-key" || r.URL.Path != "/rerank" {
				t.Error("invalid model binding")
			}
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(test.body))
		}))
		scores, e := strictRerank(context.Background(), fixture.URL, "exact-row-key", "rank", "q", []string{"candidate"})
		fixture.Close()
		if (e == nil) != test.valid {
			t.Errorf("%s: %v", test.body, e)
		}
		if test.valid && (len(scores) != 1 || scores[0] <= 0) {
			t.Fatal("single candidate normalized to zero")
		}
	}
}

func TestUploadNormalizesNFCBeforeDigestAndDuplicateCheck(t *testing.T) {
	makeUpload := func(paths []string) (*Package, error) {
		var archive bytes.Buffer
		zipped := zip.NewWriter(&archive)
		md, _ := zipped.Create("SKILL.md")
		md.Write([]byte("---\nname: orange\ndescription: okay\n---"))
		for _, path := range paths {
			f, _ := zipped.Create(path)
			f.Write([]byte("data"))
		}
		zipped.Close()
		var body bytes.Buffer
		parts := multipart.NewWriter(&body)
		parts.WriteField("manifest", `{"name":"orange","version":"1.0.0"}`)
		file, _ := parts.CreateFormFile("archive", "skill.zip")
		file.Write(archive.Bytes())
		parts.Close()
		return parsePackage(multipart.NewReader(&body, parts.Boundary()))
	}
	pkg, e := makeUpload([]string{"e\u0301.txt"})
	if e != nil || string(pkg.Data["é.txt"]) != "data" {
		t.Fatal("NFC normalization missing", e)
	}
	if _, e = makeUpload([]string{"e\u0301.txt", "é.txt"}); e == nil {
		t.Fatal("normalized duplicate accepted")
	}
	if _, _, e = spaceName("bad\u200bname"); e == nil {
		t.Fatal("format control in space name accepted")
	}
}
