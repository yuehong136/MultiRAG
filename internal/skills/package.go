package skills

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/blang/semver/v4"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
	"gopkg.in/yaml.v3"
)

const maxFile = 5 * 1024 * 1024
const maxPackage = 50 * 1024 * 1024

type FileEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}
type UploadManifest struct {
	Name     string      `json:"name"`
	Version  string      `json:"version"`
	Activate bool        `json:"activate"`
	Files    []FileEntry `json:"files"`
}
type Package struct {
	Manifest            UploadManifest
	Data                map[string][]byte
	Digest, Description string
	Tags                []string
	BinaryCount         int
	Total               int64
}

var skillName = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)

func spaceName(raw string) (string, string, error) {
	name := norm.NFC.String(strings.TrimSpace(raw))
	if !utf8.ValidString(name) || utf8.RuneCountInString(name) < 1 || utf8.RuneCountInString(name) > 128 {
		return "", "", fault(422, "INVALID_NAME")
	}
	for _, r := range name {
		if unicode.Is(unicode.C, r) {
			return "", "", fault(422, "INVALID_NAME")
		}
	}
	return name, cases.Fold().String(norm.NFC.String(name)), nil
}
func validPath(p string) error {
	if p == "" || !utf8.ValidString(p) || !norm.NFC.IsNormalString(p) || utf8.RuneCountInString(p) > 512 || strings.Contains(p, "\\") || len(p) >= 2 && p[1] == ':' {
		return fault(422, "INVALID_PATH")
	}
	for _, r := range p {
		if unicode.Is(unicode.C, r) {
			return fault(422, "INVALID_PATH")
		}
	}
	for _, part := range strings.Split(p, "/") {
		if part == "" || part == "." || part == ".." || utf8.RuneCountInString(part) > 255 {
			return fault(422, "INVALID_PATH")
		}
	}
	return nil
}
func readLimit(r io.Reader, n int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(r, n+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > n {
		return nil, fault(413, "PACKAGE_TOO_LARGE")
	}
	return data, nil
}
func parsePackage(reader *multipart.Reader) (*Package, error) {
	p := &Package{Data: map[string][]byte{}, Tags: []string{}}
	var manifestSeen bool
	var files [][]byte
	var archive []byte
	var incoming int64
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fault(422, "INVALID_MULTIPART")
		}
		limit := int64(maxFile)
		if part.FormName() == "archive" {
			limit = maxPackage
		}
		if part.FormName() == "manifest" {
			limit = 1024 * 1024
		}
		data, err := readLimit(part, limit)
		part.Close()
		if err != nil {
			return nil, err
		}
		incoming += int64(len(data))
		if incoming > maxPackage+1024*1024 {
			return nil, fault(413, "PACKAGE_TOO_LARGE")
		}
		switch part.FormName() {
		case "manifest":
			if manifestSeen {
				return nil, fault(422, "DUPLICATE_MANIFEST")
			}
			manifestSeen = true
			decoder := json.NewDecoder(bytes.NewReader(data))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&p.Manifest) != nil || decoder.Decode(&struct{}{}) != io.EOF {
				return nil, fault(422, "INVALID_MANIFEST")
			}
		case "file":
			files = append(files, data)
			if len(files) > 1000 {
				return nil, fault(413, "TOO_MANY_FILES")
			}
		case "archive":
			if archive != nil {
				return nil, fault(422, "INVALID_ARCHIVE")
			}
			archive = data
		default:
			return nil, fault(422, "INVALID_MULTIPART")
		}
	}
	if !manifestSeen || len(p.Manifest.Name) > 64 || !skillName.MatchString(p.Manifest.Name) || len(p.Manifest.Version) > 128 {
		return nil, fault(422, "INVALID_MANIFEST")
	}
	if _, err := semver.Parse(p.Manifest.Version); err != nil {
		return nil, fault(422, "INVALID_VERSION")
	}
	for i := range p.Manifest.Files {
		p.Manifest.Files[i].Path = norm.NFC.String(p.Manifest.Files[i].Path)
	}
	add := func(path string, data []byte) error {
		path = norm.NFC.String(path)
		if err := validPath(path); err != nil {
			return err
		}
		if _, ok := p.Data[path]; ok {
			return fault(422, "DUPLICATE_PATH")
		}
		p.Total += int64(len(data))
		if p.Total > maxPackage || len(p.Data) >= 1000 {
			return fault(413, "PACKAGE_TOO_LARGE")
		}
		p.Data[path] = data
		return nil
	}
	if archive != nil {
		if len(files) > 0 {
			return nil, fault(422, "MIXED_UPLOAD")
		}
		zr, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
		if err != nil {
			return nil, fault(422, "INVALID_ARCHIVE")
		}
		for _, f := range zr.File {
			if f.Flags&1 != 0 || f.Mode()&0170000 != 0 && !f.Mode().IsRegular() && !f.FileInfo().IsDir() {
				return nil, fault(422, "UNSAFE_ARCHIVE")
			}
			if f.FileInfo().IsDir() {
				if err := validPath(norm.NFC.String(strings.TrimSuffix(f.Name, "/"))); err != nil {
					return nil, err
				}
				continue
			}
			if !f.Mode().IsRegular() {
				return nil, fault(422, "UNSAFE_ARCHIVE")
			}
			r, err := f.Open()
			if err != nil {
				return nil, fault(422, "INVALID_ARCHIVE")
			}
			data, err := readLimit(r, maxFile)
			r.Close()
			if err != nil {
				return nil, err
			}
			if err = add(f.Name, data); err != nil {
				return nil, err
			}
		}
	} else {
		if len(files) != len(p.Manifest.Files) {
			return nil, fault(422, "MANIFEST_MISMATCH")
		}
		for i, data := range files {
			if err := add(p.Manifest.Files[i].Path, data); err != nil {
				return nil, err
			}
		}
	}
	if len(p.Data) == 0 {
		return nil, fault(422, "EMPTY_PACKAGE")
	}
	var canonical []FileEntry
	for path, data := range p.Data {
		sum := sha256.Sum256(data)
		canonical = append(canonical, FileEntry{path, hex.EncodeToString(sum[:]), int64(len(data))})
		if !utf8.Valid(data) {
			p.BinaryCount++
		}
	}
	sort.Slice(canonical, func(i, j int) bool { return canonical[i].Path < canonical[j].Path })
	if len(p.Manifest.Files) > 0 {
		if len(p.Manifest.Files) != len(canonical) {
			return nil, fault(422, "MANIFEST_MISMATCH")
		}
		seen := map[string]bool{}
		for _, f := range p.Manifest.Files {
			if seen[f.Path] {
				return nil, fault(422, "DUPLICATE_PATH")
			}
			seen[f.Path] = true
			data, ok := p.Data[f.Path]
			sum := sha256.Sum256(data)
			if !ok || f.Size != int64(len(data)) || f.SHA256 != hex.EncodeToString(sum[:]) {
				return nil, fault(422, "MANIFEST_MISMATCH")
			}
		}
	}
	for path := range p.Data {
		parts := strings.Split(path, "/")
		for i := 1; i < len(parts); i++ {
			if _, exists := p.Data[strings.Join(parts[:i], "/")]; exists {
				return nil, fault(422, "PATH_CONFLICT")
			}
		}
	}
	p.Manifest.Files = canonical
	h := sha256.New()
	for _, f := range canonical {
		fmt.Fprintf(h, "%s\x00%s\x00%d\n", f.Path, f.SHA256, f.Size)
	}
	p.Digest = hex.EncodeToString(h.Sum(nil))
	md, ok := p.Data["SKILL.md"]
	if !ok {
		return nil, fault(422, "SKILL_MD_REQUIRED")
	}
	var err error
	p.Description, p.Tags, err = metadata(md, p.Manifest.Name)
	return p, err
}
func metadata(md []byte, name string) (string, []string, error) {
	if !utf8.Valid(md) {
		return "", nil, fault(422, "SKILL_MD_REQUIRED")
	}
	text := strings.ReplaceAll(string(md), "\r\n", "\n")
	if !strings.HasPrefix(text, "---\n") {
		return "", nil, fault(422, "INVALID_FRONTMATTER")
	}
	lines := strings.Split(text[4:], "\n")
	end := -1
	for i, line := range lines {
		if line == "---" {
			end = i
			break
		}
	}
	if end < 0 {
		return "", nil, fault(422, "INVALID_FRONTMATTER")
	}
	var node yaml.Node
	if yaml.Unmarshal([]byte(strings.Join(lines[:end], "\n")), &node) != nil || len(node.Content) != 1 || node.Content[0].Kind != yaml.MappingNode {
		return "", nil, fault(422, "INVALID_FRONTMATTER")
	}
	var check func(*yaml.Node) bool
	check = func(n *yaml.Node) bool {
		if n.Kind == yaml.AliasNode || !strings.HasPrefix(n.Tag, "!!") {
			return false
		}
		for _, c := range n.Content {
			if !check(c) {
				return false
			}
		}
		return true
	}
	if !check(node.Content[0]) {
		return "", nil, fault(422, "UNSAFE_YAML")
	}
	mapping := node.Content[0]
	for i := 0; i < len(mapping.Content); i += 2 {
		key, value := mapping.Content[i], mapping.Content[i+1]
		switch key.Value {
		case "name", "description":
			if value.Tag != "!!str" {
				return "", nil, fault(422, "INVALID_FRONTMATTER")
			}
		case "tags":
			if value.Kind != yaml.SequenceNode {
				return "", nil, fault(422, "INVALID_FRONTMATTER")
			}
			for _, tag := range value.Content {
				if tag.Tag != "!!str" {
					return "", nil, fault(422, "INVALID_FRONTMATTER")
				}
			}
		}
	}
	var meta struct {
		Name        string   `yaml:"name"`
		Description string   `yaml:"description"`
		Tags        []string `yaml:"tags"`
	}
	if node.Content[0].Decode(&meta) != nil || meta.Name != name || strings.TrimSpace(meta.Description) == "" || utf8.RuneCountInString(meta.Description) > 4096 || len(meta.Tags) > 32 {
		return "", nil, fault(422, "INVALID_FRONTMATTER")
	}
	if meta.Tags == nil {
		meta.Tags = []string{}
	}
	return meta.Description, meta.Tags, nil
}
