package cli

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
	"multirag/internal/cli/contextengine"
)

// ServerConfig contains persistent connection credentials, never session tokens.
type ServerConfig struct {
	Host     string `yaml:"host"`
	APIToken string `yaml:"api_token,omitempty"`
	UserName string `yaml:"user_name,omitempty"`
	Password string `yaml:"password,omitempty"`
}

func configForAdmin(config *ConfigFile) *ConfigFile {
	if config.AdminServer == nil {
		return &ConfigFile{}
	}
	a := config.AdminServer
	return &ConfigFile{Host: a.Host, UserName: a.UserName, Password: a.Password}
}

func validatedServerAddress(address string) (string, int, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil || strings.TrimSpace(host) == "" || strings.ContainsAny(host, "/\\?#@ \t\n\r") {
		return "", 0, fmt.Errorf("expected server host:port")
	}
	number, err := strconv.Atoi(port)
	if err != nil || number < 1 || number > 65535 {
		return "", 0, fmt.Errorf("server port must be between 1 and 65535")
	}
	return host, number, nil
}

func newServerClient(kind string, config *ServerConfig) (*MultiRAGClient, error) {
	if config == nil {
		return nil, fmt.Errorf("server configuration required")
	}
	host, port, err := validatedServerAddress(config.Host)
	if err != nil {
		return nil, err
	}
	if config.APIToken != "" && (config.UserName != "" || config.Password != "") {
		return nil, fmt.Errorf("choose token or username/password")
	}
	if config.Password != "" && config.UserName == "" {
		return nil, fmt.Errorf("username required")
	}
	if kind == "admin" && config.APIToken != "" {
		return nil, fmt.Errorf("admin server does not accept API tokens")
	}
	client := NewMultiRAGClient(kind)
	client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.APIKey = host, port, config.APIToken
	return client, nil
}

func (c *CLI) initServers() error {
	c.activeAPI = "default"
	c.apiServers = map[string]*MultiRAGClient{}
	c.serverConfigs = map[string]*ServerConfig{}
	current := c.client
	defaultConfig := &ServerConfig{Host: net.JoinHostPort(current.HTTPClient.Host, strconv.Itoa(current.HTTPClient.Port)), APIToken: current.HTTPClient.APIKey}
	if c.args != nil {
		defaultConfig.UserName = c.args.UserName
		defaultConfig.Password = c.args.Password
	}
	if current.ServerType == "admin" {
		c.adminClient, c.adminConfig = current, defaultConfig
		defaultConfig = &ServerConfig{Host: "127.0.0.1:9380"}
		if c.args != nil && c.args.Config != nil {
			config := c.args.Config
			defaultConfig = &ServerConfig{Host: config.Host, APIToken: config.APIToken, UserName: config.UserName, Password: config.Password}
			if defaultConfig.Host == "" {
				defaultConfig.Host = "127.0.0.1:9380"
			}
		}
	} else {
		c.apiServers["default"] = current
	}
	if c.args != nil && c.args.Config != nil {
		for name, config := range c.args.Config.APIServers {
			if name == "default" || strings.TrimSpace(name) == "" {
				return fmt.Errorf("reserved or empty API server name")
			}
			client, err := newServerClient("user", config)
			if err != nil {
				return fmt.Errorf("invalid API server configuration: %w", err)
			}
			value := *config
			c.serverConfigs[name] = &value
			c.apiServers[name] = client
		}
		if current.ServerType != "admin" && c.args.Config.AdminServer != nil {
			config := *c.args.Config.AdminServer
			client, err := newServerClient("admin", &config)
			if err != nil {
				return err
			}
			c.adminClient, c.adminConfig = client, &config
		}
	}
	c.serverConfigs["default"] = defaultConfig
	if c.apiServers["default"] == nil {
		client, err := newServerClient("user", defaultConfig)
		if err != nil {
			return err
		}
		c.apiServers["default"] = client
	}
	return nil
}

// selectMode swaps whole clients, so admin credentials cannot reach an API host.
func (c *CLI) selectMode(mode string) error {
	if c.apiServers == nil {
		if err := c.initServers(); err != nil {
			return err
		}
	}
	var client *MultiRAGClient
	if mode == "admin" {
		if c.adminClient == nil {
			config := &ServerConfig{Host: "127.0.0.1:9381"}
			client, _ := newServerClient("admin", config)
			if c.adminClient != nil {
				c.adminClient.HTTPClient.client.CloseIdleConnections()
			}
			c.adminClient, c.adminConfig = client, config
		}
		client = c.adminClient
	} else {
		client = c.apiServers[c.activeAPI]
	}
	if client == nil {
		return fmt.Errorf("selected server is not configured")
	}
	c.client = client
	if c.line != nil {
		client.PasswordPrompt = c.line.PasswordPrompt
	}
	client.OutputFormat = c.outputFormat
	c.prompt = "MultiRAG(" + mode + ")> "
	engine := contextengine.NewEngine()
	engine.RegisterProvider(contextengine.NewDatasetProvider(&httpClientAdapter{client: client.HTTPClient}))
	engine.RegisterProvider(contextengine.NewFileProvider(&httpClientAdapter{client: client.HTTPClient}))
	engine.RegisterProvider(contextengine.NewSkillProvider(&skillCoreClient{client: client.HTTPClient, ctx: context.Background()}))
	c.contextEngine = engine
	return nil
}

func serverInfo(name string, config *ServerConfig, client *MultiRAGClient) map[string]interface{} {
	data := map[string]interface{}{"server": name, "configured": config != nil}
	if config == nil || client == nil {
		return data
	}
	data["host"] = net.JoinHostPort(client.HTTPClient.Host, strconv.Itoa(client.HTTPClient.Port))
	data["user_name"] = config.UserName
	data["password_configured"] = config.Password != ""
	data["api_token_configured"] = client.HTTPClient.APIKey != ""
	data["logged_in"] = client.HTTPClient.LoginToken != ""
	return data
}

// executeCommon is the single dispatcher for local configuration in both modes.
func (c *CLI) executeCommon(cmd *Command) (ResponseIf, bool, error) {
	switch cmd.Type {
	case "add_api_server", "delete_api_server", "show_api_server", "list_api_server", "add_admin_server", "delete_admin_server", "show_admin_server", "show_current", "save_config", "login_admin":
	default:
		return nil, false, nil
	}
	if c.apiServers == nil {
		if err := c.initServers(); err != nil {
			return nil, true, err
		}
	}
	name, _ := cmd.Params["name"].(string)
	switch cmd.Type {
	case "login_admin":
		if c.client.ServerType != "admin" {
			return nil, true, fmt.Errorf("LOGIN ADMIN requires admin mode")
		}
		return nil, true, c.client.LoginUser(cmd)
	case "show_current":
		config := c.serverConfigs[c.activeAPI]
		name = c.activeAPI
		if c.client.ServerType == "admin" {
			config = c.adminConfig
			name = "admin"
		}
		data := serverInfo(name, config, c.client)
		data["mode"] = c.client.ServerType
		data["output"] = c.outputFormat
		return &CommonDataResponse{Data: data}, true, nil
	case "show_admin_server":
		return &CommonDataResponse{Data: serverInfo("admin", c.adminConfig, c.adminClient)}, true, nil
	case "show_api_server":
		if c.apiServers[name] == nil {
			return nil, true, fmt.Errorf("API server does not exist")
		}
		return &CommonDataResponse{Data: serverInfo(name, c.serverConfigs[name], c.apiServers[name])}, true, nil
	case "list_api_server":
		names := make([]string, 0, len(c.apiServers))
		for name := range c.apiServers {
			names = append(names, name)
		}
		sort.Strings(names)
		rows := make([]map[string]interface{}, 0, len(names))
		for _, name := range names {
			rows = append(rows, serverInfo(name, c.serverConfigs[name], c.apiServers[name]))
		}
		return &CommonResponse{Data: rows}, true, nil
	case "add_api_server", "add_admin_server":
		host, _ := cmd.Params["host"].(string)
		token, _ := cmd.Params["token"].(string)
		kind := "user"
		if cmd.Type == "add_admin_server" {
			kind = "admin"
		} else if name == "" || c.apiServers[name] != nil {
			return nil, true, fmt.Errorf("API server name is empty or already exists")
		}
		config := &ServerConfig{Host: host, APIToken: token}
		client, err := newServerClient(kind, config)
		if err != nil {
			return nil, true, err
		}
		if kind == "admin" {
			if c.adminClient != nil && c.adminClient.HTTPClient.LoginToken != "" {
				return nil, true, fmt.Errorf("logout before replacing admin server")
			}
			c.adminClient, c.adminConfig = client, config
			if c.client.ServerType == "admin" {
				if err := c.selectMode("admin"); err != nil {
					return nil, true, err
				}
			}
		} else {
			c.apiServers[name] = client
			c.serverConfigs[name] = config
		}
	case "delete_api_server":
		if name == c.activeAPI {
			return nil, true, fmt.Errorf("cannot delete active API server")
		}
		if c.apiServers[name] == nil {
			return nil, true, fmt.Errorf("API server does not exist")
		}
		c.apiServers[name].HTTPClient.client.CloseIdleConnections()
		delete(c.apiServers, name)
		delete(c.serverConfigs, name)
	case "delete_admin_server":
		if c.adminClient == nil {
			return nil, true, fmt.Errorf("admin server is not configured")
		}
		if c.client.ServerType == "admin" || c.adminClient.HTTPClient.LoginToken != "" {
			return nil, true, fmt.Errorf("leave admin mode and logout before deletion")
		}
		c.adminClient.HTTPClient.client.CloseIdleConnections()
		c.adminClient = nil
		c.adminConfig = nil
	case "save_config":
		path, _ := cmd.Params["path"].(string)
		if err := c.saveServerConfig(path); err != nil {
			return nil, true, err
		}
	}
	return &SimpleResponse{Message: "Configuration updated"}, true, nil
}

func (c *CLI) saveServerConfig(path string) error {
	if strings.TrimSpace(path) == "" {
		return fmt.Errorf("configuration path required")
	}
	value := c.serverConfigs["default"]
	config := &ConfigFile{APIServers: map[string]*ServerConfig{}, AdminServer: c.adminConfig}
	if value != nil {
		config.Host = value.Host
		config.APIToken = value.APIToken
		config.UserName = value.UserName
		config.Password = value.Password
	}
	for name, value := range c.serverConfigs {
		if name != "default" {
			config.APIServers[name] = value
		}
	}
	// Include current host changes and API keys; session tokens never persist.
	for name, client := range c.apiServers {
		item := ServerConfig{Host: net.JoinHostPort(client.HTTPClient.Host, strconv.Itoa(client.HTTPClient.Port)), APIToken: client.HTTPClient.APIKey}
		if saved := c.serverConfigs[name]; saved != nil {
			item.UserName = saved.UserName
			item.Password = saved.Password
		}
		if name == "default" {
			config.Host = item.Host
			config.APIToken = item.APIToken
		} else {
			config.APIServers[name] = &item
		}
	}
	if c.adminClient != nil {
		item := *c.adminConfig
		item.Host = net.JoinHostPort(c.adminClient.HTTPClient.Host, strconv.Itoa(c.adminClient.HTTPClient.Port))
		config.AdminServer = &item
	}
	data, err := yaml.Marshal(config)
	if err != nil {
		return fmt.Errorf("cannot encode configuration")
	}
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("configuration target must be a regular file")
	} else if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("cannot inspect configuration target")
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".multirag-config-*")
	if err != nil {
		return fmt.Errorf("cannot create private configuration")
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if err = file.Chmod(0600); err == nil {
		_, err = file.Write(data)
	}
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("cannot write configuration")
	}
	if err = os.Rename(temporary, path); err != nil {
		return fmt.Errorf("cannot replace configuration")
	}
	return nil
}
