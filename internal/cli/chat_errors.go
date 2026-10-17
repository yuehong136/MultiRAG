package cli

import (
	"errors"
	"fmt"
	"io"
	"net"
)

// chatRequestError preserves the cause while distinguishing closed transports from timeouts.
func chatRequestError(err error) error {
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return fmt.Errorf("chat connection closed (EOF): upstream overloaded or proxy timeout: %w", err)
	}
	var timeout net.Error
	if errors.As(err, &timeout) && timeout.Timeout() {
		return fmt.Errorf("chat request timeout: model took too long to respond: %w", err)
	}
	return fmt.Errorf("failed to chat model: %w", err)
}
