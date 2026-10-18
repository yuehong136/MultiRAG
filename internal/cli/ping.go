package cli

import "strings"

func (c *MultiRAGClient) pingPath() string {
	if c.ServerType == "admin" {
		return "/admin/ping"
	}
	return "/system/ping"
}

func pingResponseOK(response *Response) bool {
	if response == nil || response.StatusCode != 200 {
		return false
	}
	if string(response.Body) == "pong" {
		return true
	}
	if err := checkAuthResponse("ping", response); err != nil {
		return false
	}
	payload, err := response.JSON()
	if err != nil {
		return false
	}
	message, _ := payload["message"].(string)
	return strings.EqualFold(message, "pong")
}
