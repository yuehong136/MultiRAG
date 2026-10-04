package cli

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"
)

const skillFileLimit = 5 << 20
const skillPackageLimit = 50 << 20

type skillUploadFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int    `json:"size"`
	data   []byte
}

func skillLocalFiles(root string) ([]skillUploadFile, error) {
	directory, err := os.OpenRoot(root)
	if err != nil {
		return nil, err
	}
	defer directory.Close()
	files := []skillUploadFile{}
	total := 0
	err = filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("skill package cannot contain symlinks")
		}
		if entry.IsDir() {
			return nil
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("skill package cannot contain special files")
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		originalPath := relative
		relative = norm.NFC.String(filepath.ToSlash(relative))
		if strings.Contains(relative, "\\") || len([]rune(relative)) > 512 || strings.IndexFunc(relative, unicode.IsControl) >= 0 {
			return fmt.Errorf("invalid skill package path")
		}
		for _, segment := range strings.Split(relative, "/") {
			if len([]rune(segment)) > 255 {
				return fmt.Errorf("skill package path segment exceeds 255 characters")
			}
		}
		originalInfo, err := entry.Info()
		if err != nil || !originalInfo.Mode().IsRegular() {
			return fmt.Errorf("package file changed while reading")
		}
		file, err := directory.Open(originalPath)
		if err != nil {
			return err
		}
		// Recheck after opening; no temporary tree is extracted or executed.
		info, statErr := file.Stat()
		if statErr != nil || !info.Mode().IsRegular() || !os.SameFile(info, originalInfo) {
			file.Close()
			return fmt.Errorf("package file changed while reading")
		}
		data, err := io.ReadAll(io.LimitReader(file, skillFileLimit+1))
		file.Close()
		if err != nil {
			return err
		}
		total += len(data)
		if len(data) > skillFileLimit || total > skillPackageLimit || len(files) >= 1000 {
			return fmt.Errorf("skill package exceeds file/count/size limits")
		}
		digest := sha256.Sum256(data)
		files = append(files, skillUploadFile{Path: relative, SHA256: hex.EncodeToString(digest[:]), Size: len(data), data: data})
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	found := false
	for i, file := range files {
		if i > 0 && files[i-1].Path == file.Path {
			return nil, fmt.Errorf("duplicate normalized package path")
		}
		found = found || file.Path == "SKILL.md"
	}
	if !found {
		return nil, fmt.Errorf("package root must contain SKILL.md")
	}
	return files, nil
}

func buildSkillUpload(path, name, version string, activate bool) (*bytes.Buffer, string, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, "", err
	}
	manifest := map[string]any{"name": name, "version": version, "activate": activate}
	var files []skillUploadFile
	var archive []byte
	if info.IsDir() {
		files, err = skillLocalFiles(path)
		if err != nil {
			return nil, "", err
		}
		manifest["files"] = files
	} else {
		if !info.Mode().IsRegular() || info.Size() > skillPackageLimit {
			return nil, "", fmt.Errorf("archive must be a regular ZIP within 50 MiB")
		}
		file, err := os.Open(path)
		if err != nil {
			return nil, "", err
		}
		archive, err = io.ReadAll(io.LimitReader(file, skillPackageLimit+1))
		file.Close()
		if err != nil || len(archive) > skillPackageLimit {
			return nil, "", fmt.Errorf("cannot read archive within size limit")
		}
	}
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	encoded, err := json.Marshal(manifest)
	if err != nil {
		return nil, "", err
	}
	if err = writer.WriteField("manifest", string(encoded)); err != nil {
		return nil, "", err
	}
	if files != nil {
		for _, file := range files {
			part, err := writer.CreateFormFile("file", filepath.Base(file.Path))
			if err != nil {
				return nil, "", err
			}
			if _, err = part.Write(file.data); err != nil {
				return nil, "", err
			}
		}
	} else {
		part, err := writer.CreateFormFile("archive", "skills.zip")
		if err != nil {
			return nil, "", err
		}
		if _, err = part.Write(archive); err != nil {
			return nil, "", err
		}
	}
	if err = writer.Close(); err != nil {
		return nil, "", err
	}
	return body, writer.FormDataContentType(), nil
}
