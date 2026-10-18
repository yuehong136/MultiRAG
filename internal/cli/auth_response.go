package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
)

// checkAuthResponse accepts the current Go and Python authentication envelopes.
// A missing business code must never be interpreted as success.
func checkAuthResponse(action string, response *Response) error {
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("%s failed: HTTP %d", action, response.StatusCode)
	}
	var result struct {
		Code    *int   `json:"code"`
		RetCode *int   `json:"retcode"`
		Message string `json:"message"`
		RetMsg  string `json:"retmsg"`
	}
	if err := json.Unmarshal(response.Body, &result); err != nil {
		return fmt.Errorf("%s failed: invalid JSON response: %w", action, err)
	}
	code := result.Code
	message := result.Message
	if code == nil {
		code = result.RetCode
		message = result.RetMsg
	}
	if code == nil || (result.Code != nil && result.RetCode != nil && *result.Code != *result.RetCode) {
		return fmt.Errorf("%s failed: invalid business code", action)
	}
	if *code != 0 {
		return fmt.Errorf("%s failed: %s", action, message)
	}
	return nil
}
