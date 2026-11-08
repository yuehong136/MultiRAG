//go:build !office_oxide

package parser

import "fmt"

func parseOfficeOxide(format string, _ []byte) (*Result, error) {
	return nil, fmt.Errorf("%w: %s requires office_oxide native build", ErrUnsupported, format)
}
