//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package cli

import (
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	"github.com/peterh/liner"
	"gopkg.in/yaml.v3"
)

// HistoryFile returns the path to the history file
func HistoryFile() string {
	return os.Getenv("HOME") + "/" + historyFileName
}

const historyFileName = ".multirag_cli_history"

// configFileName is the default connection config read from the current directory.
const configFileName = "multirag.yml"

// OutputFormat represents the output format type
type OutputFormat string

const (
	OutputFormatTable OutputFormat = "table" // Table format with borders
	OutputFormatPlain OutputFormat = "plain" // Plain text, space-separated (no borders)
	OutputFormatJSON  OutputFormat = "json"  // JSON format
)

// validateOutputFormat reports whether format is one of the supported formats.
func validateOutputFormat(format string) error {
	switch OutputFormat(format) {
	case OutputFormatTable, OutputFormatPlain, OutputFormatJSON:
		return nil
	default:
		return fmt.Errorf("invalid output format: %s (expected table, plain or json)", format)
	}
}

// ConfigFile represents the multirag.yml connection config file structure.
type ConfigFile struct {
	Host     string `yaml:"host"`
	APIToken string `yaml:"api_token"`
	UserName string `yaml:"user_name"`
	Password string `yaml:"password"`
}

// ConnectionArgs holds the parsed command line / config-file connection options.
type ConnectionArgs struct {
	Host         string
	Port         int
	Password     string
	APIToken     string
	UserName     string
	Command      string // single command to run non-interactively (empty -> REPL)
	OutputFormat string
	ShowHelp     bool
	AdminMode    bool
}

// LoadDefaultConfigFile reads multirag.yml from the current directory if present.
// It returns (nil, nil) when the file does not exist.
func LoadDefaultConfigFile() (*ConfigFile, error) {
	data, err := os.ReadFile(configFileName)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	var config ConfigFile
	if err = yaml.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("failed to parse %s: %v", configFileName, err)
	}
	return &config, nil
}

// LoadConfigFileFromPath reads a connection config file from the given path.
func LoadConfigFileFromPath(path string) (*ConfigFile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read config file %s: %v", path, err)
	}

	var config ConfigFile
	if err = yaml.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("failed to parse config file %s: %v", path, err)
	}
	return &config, nil
}

// parseHostPort splits a "host:port" string into its host and port parts.
func parseHostPort(hostPort string) (string, int, error) {
	if hostPort == "" {
		return "", -1, nil
	}

	parts := strings.Split(hostPort, ":")
	if len(parts) != 2 {
		return "", -1, fmt.Errorf("invalid host format, expected host:port, got: %s", hostPort)
	}

	host := parts[0]
	port, err := strconv.Atoi(parts[1])
	if err != nil {
		return "", -1, fmt.Errorf("invalid port number: %s", parts[1])
	}
	return host, port, nil
}

// ParseConnectionArgs parses CLI connection options with priority
// command line > config file > defaults. Authentication is either an API token
// (-t/--token) or username/password (-u/--user, -p/--password); the two are
// mutually exclusive. Admin mode (--admin) ignores the config file.
func ParseConnectionArgs(args []string) (*ConnectionArgs, error) {
	// First pass: help, config file path and admin mode.
	var configFilePath string
	var adminMode bool
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--help" || arg == "-help":
			return &ConnectionArgs{ShowHelp: true}, nil
		case (arg == "-f" || arg == "--config") && i+1 < len(args):
			configFilePath = args[i+1]
			i++
		case arg == "--admin" || arg == "-admin":
			adminMode = true
		}
	}

	result := &ConnectionArgs{}

	// Apply config file first (lower priority). Admin mode ignores it, since the
	// config carries user-mode auth (user_name/password/api_token).
	if !adminMode {
		var config *ConfigFile
		var err error
		if configFilePath != "" {
			config, err = LoadConfigFileFromPath(configFilePath)
		} else {
			config, err = LoadDefaultConfigFile()
		}
		if err != nil {
			return nil, err
		}
		if config != nil {
			if config.Host != "" {
				h, port, err := parseHostPort(config.Host)
				if err != nil {
					return nil, fmt.Errorf("invalid host in config file: %v", err)
				}
				result.Host = h
				result.Port = port
			}
			result.UserName = config.UserName
			result.Password = config.Password
			result.APIToken = config.APIToken
		}
	}

	// Override with command line flags (higher priority). Both short and long
	// forms are supported.
	var outputFormat string
	var nonFlagArgs []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch arg {
		case "-h", "--host":
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				h, port, err := parseHostPort(args[i+1])
				if err != nil {
					return nil, fmt.Errorf("invalid host format: %v", err)
				}
				result.Host = h
				result.Port = port
				i++
			}
		case "-t", "--token":
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				result.APIToken = args[i+1]
				i++
			}
		case "-u", "--user":
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				result.UserName = args[i+1]
				i++
			}
		case "-p", "--password":
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				result.Password = args[i+1]
				i++
			}
		case "-o", "--output":
			if i+1 < len(args) {
				outputFormat = args[i+1]
				i++
			}
		case "-f", "--config":
			// Config file path already parsed above.
			if i+1 < len(args) {
				i++
			}
		case "--admin", "-admin":
			result.AdminMode = true
		case "--help", "-help":
			continue
		default:
			// Non-flag argument (command word).
			if !strings.HasPrefix(arg, "-") {
				nonFlagArgs = append(nonFlagArgs, arg)
			}
		}
	}

	if outputFormat != "" {
		if err := validateOutputFormat(outputFormat); err != nil {
			return nil, err
		}
		result.OutputFormat = outputFormat
	}

	// Defaults. Ports are MultiRAG's actual listeners: 9381 (admin) / 9380 (user).
	if result.Host == "" {
		result.Host = "127.0.0.1"
	}
	if result.Port == -1 || result.Port == 0 {
		if result.AdminMode {
			result.Port = 9381
		} else {
			result.Port = 9380
		}
	}

	if result.UserName == "" && result.Password != "" {
		return nil, fmt.Errorf("username (-u/--user) is required when using a password (-p/--password)")
	}

	if result.AdminMode {
		// Admin uses the superuser login; API tokens do not apply.
		result.APIToken = ""
		if result.UserName == "" {
			result.UserName = "admin@multirag.com"
			result.Password = ""
		}
	} else {
		// User mode: API token and username/password are mutually exclusive.
		hasToken := result.APIToken != ""
		hasUserPass := result.UserName != "" || result.Password != ""
		if hasToken && hasUserPass {
			return nil, fmt.Errorf("cannot use both API token (-t/--token) and username/password (-u/--user, -p/--password); use one authentication method")
		}
	}

	// Single command to run non-interactively. Our parser expects a trailing
	// semicolon, so append one when missing.
	if len(nonFlagArgs) > 0 {
		cmd := strings.Join(nonFlagArgs, " ")
		if !strings.HasSuffix(strings.TrimSpace(cmd), ";") {
			cmd += ";"
		}
		result.Command = cmd
	}

	return result, nil
}

// PrintUsage prints the CLI usage information.
func PrintUsage() {
	fmt.Print(`MultiRAG CLI Client

Usage: multirag_cli [options] [command]

Options:
  -h, --host string      MultiRAG service address (host:port, default "127.0.0.1:9380")
  -t, --token string     API token for authentication
  -u, --user string      Username for authentication
  -p, --password string  Password for authentication
  -o, --output string    Output format: table (default), plain or json
  -f, --config string    Path to config file (YAML format)
  --admin, -admin        Run in admin mode
  --help                 Show this help message

Mode:
  --admin, -admin        Run in admin mode (prompt: MultiRAG(admin)>)
  Default is user mode (prompt: MultiRAG(user)>).

Authentication:
  You can authenticate using either:
    1. API token: -t or --token
    2. Username and password: -u/--user and -p/--password
  Note: these two methods are mutually exclusive.

Configuration File:
  The CLI automatically reads multirag.yml from the current directory if it exists.
  Use -f or --config to specify a custom config file path.
  Command line options override config file values.
  Admin mode (--admin) ignores the config file.

  Config file format:
    host: 127.0.0.1:9380
    api_token: your-api-token
    user_name: your-username
    password: your-password

  Note: api_token and user_name/password are mutually exclusive in the config file.

Commands:
  SQL commands like: LOGIN USER 'email'; LIST USERS; etc.
  If no command is provided, the CLI runs in interactive mode.
`)
}

// CLI represents the command line interface
type CLI struct {
	client       *MultiRAGClient
	prompt       string
	running      bool
	line         *liner.State
	outputFormat OutputFormat
	args         *ConnectionArgs
}

// NewCLI creates a new CLI instance in interactive mode with default settings.
func NewCLI() (*CLI, error) {
	return NewCLIWithArgs(nil)
}

// NewCLIWithArgs creates a new CLI instance, applying the given connection
// arguments when provided. A nil args is treated as "interactive, user mode,
// defaults".
func NewCLIWithArgs(args *ConnectionArgs) (*CLI, error) {
	// Create liner first
	line := liner.NewLiner()

	// Determine server type from the --admin flag; default to user mode.
	serverType := "user"
	if args != nil && args.AdminMode {
		serverType = "admin"
	}

	// Create client with password prompt using liner
	client := NewMultiRAGClient(serverType)
	client.PasswordPrompt = line.PasswordPrompt

	// Apply connection arguments when provided. NewMultiRAGClient already set the
	// default port for the mode, so only override when explicitly given.
	if args != nil {
		if args.Host != "" {
			client.HTTPClient.Host = args.Host
		}
		if args.Port > 0 {
			client.HTTPClient.Port = args.Port
		}
		if args.APIToken != "" {
			client.HTTPClient.APIKey = args.APIToken
		}
	}

	prompt := "MultiRAG(user)> "
	if serverType == "admin" {
		prompt = "MultiRAG(admin)> "
	}

	outputFormat := OutputFormatTable
	if args != nil && args.OutputFormat != "" {
		outputFormat = OutputFormat(args.OutputFormat)
	}

	return &CLI{
		prompt:       prompt,
		client:       client,
		line:         line,
		outputFormat: outputFormat,
		args:         args,
	}, nil
}

// Run starts the interactive CLI
func (c *CLI) Run() error {
	// When a username was given without a password (admin mode defaults a
	// username, or -u without -p), prompt for the password and verify before
	// entering the REPL.
	if c.args != nil && c.args.UserName != "" && c.args.Password == "" && c.args.APIToken == "" {
		if err := c.verifyPassword(); err != nil {
			return err
		}
	}

	c.running = true

	// Load history from file
	histFile := HistoryFile()
	if f, err := os.Open(histFile); err == nil {
		c.line.ReadHistory(f)
		f.Close()
	}

	// Save history on exit
	defer func() {
		if f, err := os.Create(histFile); err == nil {
			c.line.WriteHistory(f)
			f.Close()
		}
		c.line.Close()
	}()

	fmt.Println("Welcome to MultiRAG CLI")
	fmt.Println("Type \\? for help, \\q to quit")
	fmt.Println()

	for c.running {
		input, err := c.line.Prompt(c.prompt)
		if err != nil {
			fmt.Printf("Error reading input: %v\n", err)
			continue
		}

		input = strings.TrimSpace(input)

		if input == "" {
			continue
		}

		// Add to history (skip meta commands)
		if !strings.HasPrefix(input, "\\") {
			c.line.AppendHistory(input)
		}

		if err := c.execute(input); err != nil {
			fmt.Printf("Error: %v\n", err)
		}
	}

	return nil
}

func (c *CLI) execute(input string) error {
	p := NewParser(input)
	cmd, err := p.Parse(c.client.ServerType == "admin")
	if err != nil {
		return err
	}

	if cmd == nil {
		return nil
	}

	// Handle meta commands
	if cmd.Type == "meta" {
		return c.handleMetaCommand(cmd)
	}

	// Execute the command using the client; the returned response renders itself
	// in the currently selected output format.
	result, err := c.client.ExecuteCommand(cmd)
	if result != nil {
		result.SetOutputFormat(c.outputFormat)
		result.PrintOut()
	}
	return err
}

func (c *CLI) handleMetaCommand(cmd *Command) error {
	command := cmd.Params["command"].(string)
	args, _ := cmd.Params["args"].([]string)

	switch command {
	case "q", "quit", "exit":
		fmt.Println("Goodbye!")
		c.running = false
	case "?", "h", "help":
		c.printHelp()
	case "c", "clear":
		// Clear screen (simple approach)
		fmt.Print("\033[H\033[2J")
	case "admin":
		c.client.ServerType = "admin"
		c.client.HTTPClient.Port = 9381
		c.prompt = "MultiRAG(admin)> "
		fmt.Println("Switched to ADMIN mode (port 9381)")
	case "user":
		c.client.ServerType = "user"
		c.client.HTTPClient.Port = 9380
		c.prompt = "MultiRAG(user)> "
		fmt.Println("Switched to USER mode (port 9380)")
	case "host":
		if len(args) == 0 {
			fmt.Printf("Current host: %s\n", c.client.HTTPClient.Host)
		} else {
			c.client.HTTPClient.Host = args[0]
			fmt.Printf("Host set to: %s\n", args[0])
		}
	case "port":
		if len(args) == 0 {
			fmt.Printf("Current port: %d\n", c.client.HTTPClient.Port)
		} else {
			port, err := strconv.Atoi(args[0])
			if err != nil {
				return fmt.Errorf("invalid port number: %s", args[0])
			}
			if port < 1 || port > 65535 {
				return fmt.Errorf("port must be between 1 and 65535")
			}
			c.client.HTTPClient.Port = port
			fmt.Printf("Port set to: %d\n", port)
		}
	case "status":
		fmt.Printf("Server: %s:%d (mode: %s)\n", c.client.HTTPClient.Host, c.client.HTTPClient.Port, c.client.ServerType)
	case "format", "f":
		if len(args) == 0 {
			fmt.Printf("Current output format: %s\n", c.outputFormat)
		} else {
			switch strings.ToLower(args[0]) {
			case "table":
				c.outputFormat = OutputFormatTable
			case "plain":
				c.outputFormat = OutputFormatPlain
			case "json":
				c.outputFormat = OutputFormatJSON
			default:
				return fmt.Errorf("invalid output format: %s (expected table, plain or json)", args[0])
			}
			fmt.Printf("Output format set to: %s\n", c.outputFormat)
		}
	default:
		return fmt.Errorf("unknown meta command: \\%s", command)
	}
	return nil
}

func (c *CLI) printHelp() {
	help := `
MultiRAG CLI Help
================

Meta Commands:
  \admin        - Switch to ADMIN mode (port 9381)
  \user         - Switch to USER mode (port 9380)
  \host [ip]    - Show or set server host (default: 127.0.0.1)
  \port [num]   - Show or set server port (default: 9380 for user, 9381 for admin)
  \format [fmt] - Show or set output format: table (default), plain, json
  \status       - Show current connection status
  \? or \h      - Show this help
  \q or \quit   - Exit CLI
  \c or \clear  - Clear screen

SQL Commands (User Mode):
  LOGIN USER 'email';                                    - Login as user
  LOGOUT;                                                - End the current session
  REGISTER USER 'name' AS 'nickname' PASSWORD 'pwd';     - Register new user
  SHOW VERSION;                                          - Show version info
  PING;                                                  - Ping server
  LIST DATASETS;                                         - List user datasets
  LIST AGENTS;                                           - List user agents
  LIST CHATS;                                            - List user chats
  LIST MODEL PROVIDERS;                                  - List model providers
  LIST DEFAULT MODELS;                                   - List default models
  CREATE INDEX FOR DATASET 'name' VECTOR_SIZE N;         - Create index for dataset
  DROP INDEX FOR DATASET 'name';                         - Drop index for dataset
  CREATE INDEX DOC_META;                                 - Create doc meta index
  DROP INDEX DOC_META;                                   - Drop doc meta index

SQL Commands (Admin Mode):
  LOGIN USER 'email';                                    - Login as admin
  LOGOUT;                                                - End the current session
  LIST USERS;                                            - List all users
  SHOW USER 'email';                                     - Show user details
  CREATE USER 'email' 'password';                        - Create new user
  DROP USER 'email';                                     - Delete user
  ALTER USER PASSWORD 'email' 'new_password';            - Change user password
  ALTER USER ACTIVE 'email' on/off;                      - Activate/deactivate user
  GRANT ADMIN 'email';                                   - Grant admin role
  REVOKE ADMIN 'email';                                  - Revoke admin role
  LIST SERVICES;                                         - List services
  SHOW SERVICE <id>;                                     - Show service details
  PING;                                                  - Ping server
  ... and many more

Meta Commands:
  \? or \h      - Show this help
  \q or \quit   - Exit CLI
  \c or \clear  - Clear screen

For more information, see documentation.
`
	fmt.Println(help)
}

// Cleanup performs cleanup before exit
func (c *CLI) Cleanup() {
	fmt.Println("\nCleaning up...")
}

// RunInteractive runs the CLI in interactive mode
func RunInteractive() error {
	cli, err := NewCLI()
	if err != nil {
		return fmt.Errorf("failed to create CLI: %v", err)
	}

	// Handle interrupt signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigChan
		cli.Cleanup()
		os.Exit(0)
	}()

	return cli.Run()
}

// RunSingleCommand executes a single command non-interactively and returns.
func (c *CLI) RunSingleCommand(command string) error {
	return c.execute(command)
}

// verifyPassword prompts for the password and verifies it, allowing up to 3
// attempts. Used when a username is supplied without a password at startup.
func (c *CLI) verifyPassword() error {
	const maxAttempts = 3
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		var input string
		var err error

		if isTerminal() {
			input, err = c.line.PasswordPrompt("Please input your password: ")
		} else {
			fmt.Println("Warning: this terminal does not support secure password input")
			input, err = c.line.Prompt("Please input your password (will be visible): ")
		}
		if err != nil {
			fmt.Printf("Error reading input: %v\n", err)
			return err
		}

		input = strings.TrimSpace(input)
		if input == "" {
			if attempt < maxAttempts {
				fmt.Println("Password cannot be empty, please try again")
				continue
			}
			return errors.New("no password provided after 3 attempts")
		}

		c.args.Password = input
		if err = c.VerifyAuth(); err != nil {
			if attempt < maxAttempts {
				fmt.Printf("Authentication failed: %v (%d/%d attempts)\n", err, attempt, maxAttempts)
				continue
			}
			return fmt.Errorf("authentication failed after %d attempts: %v", maxAttempts, err)
		}
		return nil
	}
	return nil
}

// VerifyAuth logs in using the connection arguments to verify the credentials,
// storing the resulting session token on the client when successful.
func (c *CLI) VerifyAuth() error {
	if c.args == nil {
		return nil
	}

	// API token auth is applied at client construction; nothing to verify here.
	if c.args.APIToken != "" {
		return nil
	}

	if c.args.UserName == "" {
		return errors.New("username is required")
	}
	if c.args.Password == "" {
		return errors.New("password is required")
	}

	token, err := c.client.loginUser(c.args.UserName, c.args.Password)
	if err != nil {
		return err
	}
	c.client.HTTPClient.LoginToken = token
	return nil
}
