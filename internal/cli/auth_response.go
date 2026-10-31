package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
)

// authError keeps the cause available to callers without echoing response or
// transport text that may contain credentials.
type authError struct {
	message string
	cause   error
}

func (e *authError) Error() string { return e.message }
func (e *authError) Unwrap() error { return e.cause }

// checkAuthResponse accepts the current Go and Python authentication envelopes.
// A missing business code must never be interpreted as success.
func checkAuthResponse(action string, response *Response) error {
	return checkResponse(action, response, false)
}

func checkLoginResponse(response *Response) error {
	return checkResponse("login", response, true)
}

func checkResponse(action string, response *Response, redact bool) error {
	failure := func(detail string, cause error) error {
		message := action + " failed: " + detail
		if redact {
			return &authError{message: message, cause: cause}
		}
		if cause != nil {
			return fmt.Errorf("%s: %w", message, cause)
		}
		return errors.New(message)
	}
	if response == nil {
		return failure("missing response", nil)
	}
	if response.StatusCode != http.StatusOK {
		return failure(fmt.Sprintf("HTTP %d", response.StatusCode), nil)
	}
	var result struct {
		Code    *int   `json:"code"`
		RetCode *int   `json:"retcode"`
		Message string `json:"message"`
		RetMsg  string `json:"retmsg"`
	}
	if err := json.Unmarshal(response.Body, &result); err != nil {
		return failure("invalid JSON response", err)
	}
	code := result.Code
	message := result.Message
	if code == nil {
		code = result.RetCode
		message = result.RetMsg
	}
	if code == nil || (result.Code != nil && result.RetCode != nil && *result.Code != *result.RetCode) {
		return failure("invalid business code", nil)
	}
	if *code != 0 {
		if redact {
			message = fmt.Sprintf("business code %d", *code)
		}
		return failure(message, nil)
	}
	return nil
}
