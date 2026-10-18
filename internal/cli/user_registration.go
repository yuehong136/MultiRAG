package cli

import "fmt"

// RegisterUser creates a regular account through the public registration API.
func (c *MultiRAGClient) RegisterUser(cmd *Command) (ResponseIf, error) {
	email, ok := cmd.Params["user_name"].(string)
	if !ok || email == "" {
		return nil, fmt.Errorf("no email")
	}
	password, ok := cmd.Params["password"].(string)
	if !ok || password == "" {
		return nil, fmt.Errorf("no password")
	}
	nickname, ok := cmd.Params["nickname"].(string)
	if !ok {
		return nil, fmt.Errorf("no nickname")
	}
	encryptedPassword, err := EncryptPassword(password)
	if err != nil {
		return nil, fmt.Errorf("failed to encrypt password: %w", err)
	}
	payload := map[string]interface{}{"email": email, "password": encryptedPassword, "nickname": nickname}
	response, err := c.HTTPClient.Request("POST", "/users", "", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to register user: %w", err)
	}
	if err := checkAuthResponse("register user", response); err != nil {
		return nil, err
	}
	return &RegisterResponse{Code: 0, Message: "Register successfully"}, nil
}
