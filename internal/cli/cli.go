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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"unicode/utf8"

	"github.com/peterh/liner"
	"golang.org/x/term"
	"gopkg.in/yaml.v3"

	"multirag/internal/cli/contextengine"
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
	Command      string   // single command to run non-interactively (empty -> REPL)
	CommandArgs  []string // split arguments for Context Engine mode (ls/search/cat)
	IsSQLMode    bool     // true -> SQL parser; false -> Context Engine command
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
	// First pass: help, config file path and admin mode. Once a command word
	// (non-flag arg) appears, stop treating "--help" as global help so that a
	// subcommand like "search --help" handles its own help.
	var configFilePath string
	var adminMode bool
	foundCommand := false
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "-") {
			foundCommand = true
			continue
		}
		switch {
		case !foundCommand && (arg == "--help" || arg == "-help"):
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
	cmdStarted := false
	for i := 0; i < len(args); i++ {
		arg := args[i]
		// Once the command word is reached, the rest belongs to the subcommand
		// (including its own flags like -d/-q/-k/-t/-n).
		if cmdStarted {
			nonFlagArgs = append(nonFlagArgs, arg)
			continue
		}
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
			// Non-flag argument (command word). Everything after it belongs to
			// the command/subcommand.
			if !strings.HasPrefix(arg, "-") {
				nonFlagArgs = append(nonFlagArgs, arg)
				cmdStarted = true
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

	// Single command to run non-interactively. Decide between SQL mode and
	// Context Engine mode based on the leading word.
	if len(nonFlagArgs) > 0 {
		// Use the joined command so the "search ... ON DATASETS" disambiguation
		// (inside looksLikeContextEngine) sees the whole command, not just the verb.
		if looksLikeContextEngine(strings.Join(nonFlagArgs, " ")) {
			// Context Engine command (ls/search/cat): keep the args split so the
			// subcommand flag parser (-d/-q/-k/-t/-n) sees them, and do not append
			// a trailing semicolon.
			result.IsSQLMode = false
			result.CommandArgs = nonFlagArgs
			result.Command = strings.Join(nonFlagArgs, " ")
		} else {
			// SQL command. Our parser expects a trailing semicolon.
			result.IsSQLMode = true
			cmd := strings.Join(nonFlagArgs, " ")
			if !strings.HasSuffix(strings.TrimSpace(cmd), ";") {
				cmd += ";"
			}
			result.Command = cmd
		}
	}

	return result, nil
}

// looksLikeContextEngine reports whether the leading word selects a Context
// Engine command (ls/search/cat). Everything else is treated as a SQL command.
// Notes:
//   - "list" is intentionally excluded so that "LIST DATASETS" stays SQL.
//   - "search" collides with the SQL command `SEARCH '...' ON DATASETS '...'`;
//     it is routed to SQL when that `ON DATASETS` clause is present, otherwise CE.
func looksLikeContextEngine(input string) bool {
	fields := strings.Fields(strings.TrimSpace(input))
	if len(fields) == 0 {
		return false
	}
	switch strings.ToLower(fields[0]) {
	case "ls", "cat":
		return true
	case "search":
		return !strings.Contains(strings.ToUpper(input), " ON DATASETS")
	}
	return false
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
  Context Engine commands (no quotes): ls datasets, search -q "kw", cat path, etc.
  If no command is provided, the CLI runs in interactive mode.
`)
}

// CLI represents the command line interface
type CLI struct {
	client        *MultiRAGClient
	contextEngine *contextengine.Engine
	prompt        string
	running       bool
	line          *liner.State
	outputFormat  OutputFormat
	args          *ConnectionArgs
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

	// Auto-login when both username and password are supplied (and no API token).
	// This makes single-command mode (and config-file credentials) authenticated
	// without an interactive prompt. The "username without password" case is still
	// handled by Run() -> verifyPassword().
	if args != nil && args.UserName != "" && args.Password != "" && args.APIToken == "" {
		if err := client.LoginUserInteractive(args.UserName, args.Password); err != nil {
			line.Close()
			return nil, fmt.Errorf("auto-login failed: %w", err)
		}
	}

	outputFormat := OutputFormatTable
	if args != nil && args.OutputFormat != "" {
		outputFormat = OutputFormat(args.OutputFormat)
	}
	client.OutputFormat = outputFormat

	// Build the Context Engine and register its providers. They issue requests
	// through the CLI's HTTP client (auto auth) using existing RESTful APIs.
	engine := contextengine.NewEngine()
	engine.RegisterProvider(contextengine.NewDatasetProvider(&httpClientAdapter{client: client.HTTPClient}))
	engine.RegisterProvider(contextengine.NewFileProvider(&httpClientAdapter{client: client.HTTPClient}))

	return &CLI{
		prompt:        prompt,
		client:        client,
		contextEngine: engine,
		line:          line,
		outputFormat:  outputFormat,
		args:          args,
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
	input = strings.TrimSpace(input)
	if input == "" {
		return nil
	}

	// Meta commands start with a backslash.
	if strings.HasPrefix(input, "\\") {
		p := NewParser(input)
		cmd, err := p.Parse(c.client.ServerType == "admin")
		if err != nil {
			return err
		}
		if cmd != nil && cmd.Type == "meta" {
			return c.handleMetaCommand(cmd)
		}
		return nil
	}

	// Decide between SQL mode and Context Engine mode.
	contextEngineMode := false
	if c.args != nil && len(c.args.CommandArgs) > 0 {
		// Non-interactive single command: use the mode decided at parse time.
		contextEngineMode = !c.args.IsSQLMode
	} else {
		contextEngineMode = looksLikeContextEngine(input)
	}

	if contextEngineMode {
		return c.executeContextEngine(input)
	}

	// SQL mode: parse and run through the client. The returned response renders
	// itself in the currently selected output format.
	p := NewParser(input)
	cmd, err := p.Parse(c.client.ServerType == "admin")
	if err != nil {
		return err
	}
	if cmd == nil {
		return nil
	}
	if cmd.Type == "meta" {
		return c.handleMetaCommand(cmd)
	}
	result, err := c.client.ExecuteCommand(cmd)
	if result != nil {
		result.SetOutputFormat(c.outputFormat)
		result.PrintOut()
	}
	return err
}

// executeContextEngine runs a Context Engine command (ls/search/cat). In
// non-interactive mode the arguments come pre-split from ParseConnectionArgs;
// in interactive mode the raw input line is tokenized here.
func (c *CLI) executeContextEngine(input string) error {
	var args []string
	if c.args != nil && len(c.args.CommandArgs) > 0 {
		args = c.args.CommandArgs
	} else {
		args = parseContextEngineArgs(input)
	}

	if len(args) == 0 {
		return fmt.Errorf("no command provided")
	}
	if c.contextEngine == nil {
		return fmt.Errorf("context engine not available")
	}

	cmdType := strings.ToLower(args[0])
	cmdArgs := args[1:]

	var ceCmd *contextengine.Command

	switch cmdType {
	case "ls", "list":
		listOpts, err := parseListCommandArgs(cmdArgs)
		if err != nil {
			return err
		}
		if listOpts == nil {
			// Help was printed.
			return nil
		}
		ceCmd = &contextengine.Command{
			Type: contextengine.CommandList,
			Path: listOpts.Path,
			Params: map[string]interface{}{
				"limit": listOpts.Limit,
			},
		}
	case "search":
		searchOpts, err := parseSearchCommandArgs(cmdArgs)
		if err != nil {
			return err
		}
		if searchOpts == nil {
			// Help was printed.
			return nil
		}
		// Use the first directory for provider resolution; default to datasets.
		searchPath := "datasets"
		if len(searchOpts.Dirs) > 0 {
			searchPath = searchOpts.Dirs[0]
		}
		ceCmd = &contextengine.Command{
			Type: contextengine.CommandSearch,
			Path: searchPath,
			Params: map[string]interface{}{
				"query":     searchOpts.Query,
				"top_k":     searchOpts.TopK,
				"threshold": searchOpts.Threshold,
				"dirs":      searchOpts.Dirs,
			},
		}
	case "cat":
		if len(cmdArgs) == 0 {
			return fmt.Errorf("cat requires a path argument")
		}
		// cat returns raw bytes rather than a *Result.
		content, err := c.contextEngine.Cat(context.Background(), cmdArgs[0])
		if err != nil {
			return err
		}
		if len(content) == 0 {
			fmt.Println("(empty file)")
		} else if isBinaryContent(content) {
			return fmt.Errorf("cannot display binary file content")
		} else {
			fmt.Println(string(content))
		}
		return nil
	default:
		return fmt.Errorf("unknown context engine command: %s", cmdType)
	}

	result, err := c.contextEngine.Execute(context.Background(), ceCmd)
	if err != nil {
		return err
	}

	// search defaults to JSON output unless plain/table was explicitly selected.
	format := c.outputFormat
	if ceCmd.Type == contextengine.CommandSearch && format != OutputFormatPlain && format != OutputFormatTable {
		format = OutputFormatJSON
	}
	limit := 0
	if ceCmd.Type == contextengine.CommandList {
		if l, ok := ceCmd.Params["limit"].(int); ok {
			limit = l
		}
	}
	c.printContextEngineResult(result, ceCmd.Type, format, limit)
	return nil
}

// parseContextEngineArgs splits an input line into arguments, honoring single
// and double quotes so that quoted queries stay intact.
func parseContextEngineArgs(input string) []string {
	var args []string
	var current strings.Builder
	inQuote := false
	var quoteChar rune

	for _, ch := range input {
		switch ch {
		case '"', '\'':
			if !inQuote {
				inQuote = true
				quoteChar = ch
				if current.Len() > 0 {
					args = append(args, current.String())
					current.Reset()
				}
			} else if ch == quoteChar {
				inQuote = false
				args = append(args, current.String())
				current.Reset()
			} else {
				current.WriteRune(ch)
			}
		case ' ', '\t':
			if inQuote {
				current.WriteRune(ch)
			} else if current.Len() > 0 {
				args = append(args, current.String())
				current.Reset()
			}
		default:
			current.WriteRune(ch)
		}
	}

	if current.Len() > 0 {
		args = append(args, current.String())
	}

	return args
}

// printContextEngineResult renders the result of an ls/search command.
func (c *CLI) printContextEngineResult(result *contextengine.Result, cmdType contextengine.CommandType, format OutputFormat, limit int) {
	if result == nil {
		return
	}

	switch cmdType {
	case contextengine.CommandList:
		if len(result.Nodes) == 0 {
			fmt.Println("(empty)")
			return
		}
		displayCount := len(result.Nodes)
		if limit > 0 && displayCount > limit {
			displayCount = limit
		}
		if format == OutputFormatPlain {
			for i := 0; i < displayCount; i++ {
				node := result.Nodes[i]
				fmt.Printf("%s %s %s %s\n", node.Name, node.Type, node.Path, node.CreatedAt.Format("2006-01-02 15:04"))
			}
		} else {
			fmt.Printf("%-30s %-12s %-50s %-20s\n", "NAME", "TYPE", "PATH", "CREATED")
			fmt.Println(strings.Repeat("-", 112))
			for i := 0; i < displayCount; i++ {
				node := result.Nodes[i]
				created := node.CreatedAt.Format("2006-01-02 15:04")
				if node.CreatedAt.IsZero() {
					created = "-"
				}
				displayPath := strings.TrimPrefix(node.Path, "/")
				fmt.Printf("%-30s %-12s %-50s %-20s\n", node.Name, node.Type, displayPath, created)
			}
		}
		if limit > 0 && result.Total > limit {
			fmt.Printf("\n... and %d more (use -n to show more)\n", result.Total-limit)
		}
		fmt.Printf("Total: %d\n", result.Total)
	case contextengine.CommandSearch:
		if len(result.Nodes) == 0 {
			if format == OutputFormatJSON {
				fmt.Println("[]")
			} else {
				fmt.Println("No results found")
			}
			return
		}
		type searchResult struct {
			Content string  `json:"content"`
			Path    string  `json:"path"`
			Score   float64 `json:"score,omitempty"`
		}
		results := make([]searchResult, 0, len(result.Nodes))
		for _, node := range result.Nodes {
			content := node.Name
			if content == "" {
				content = "(empty)"
			}
			displayPath := strings.TrimPrefix(node.Path, "/")
			var score float64
			if s, ok := node.Metadata["similarity"].(float64); ok {
				score = s
			} else if s, ok := node.Metadata["_score"].(float64); ok {
				score = s
			}
			results = append(results, searchResult{
				Content: content,
				Path:    displayPath,
				Score:   score,
			})
		}
		if format == OutputFormatJSON {
			jsonData, err := json.MarshalIndent(results, "", "  ")
			if err != nil {
				fmt.Printf("Error marshaling JSON: %v\n", err)
				return
			}
			fmt.Println(string(jsonData))
		} else if format == OutputFormatPlain {
			fmt.Printf("%-70s  %-50s  %-10s\n", "CONTENT", "PATH", "SCORE")
			for i, sr := range results {
				content := strings.Join(strings.Fields(sr.Content), " ")
				if len(content) > 70 {
					content = content[:67] + "..."
				}
				displayPath := sr.Path
				if len(displayPath) > 50 {
					displayPath = displayPath[:47] + "..."
				}
				scoreStr := "-"
				if sr.Score > 0 {
					scoreStr = fmt.Sprintf("%.4f", sr.Score)
				}
				fmt.Printf("%-70s  %-50s  %-10s\n", content, displayPath, scoreStr)
				if i >= 99 {
					fmt.Printf("\n... and %d more results\n", result.Total-i-1)
					break
				}
			}
			fmt.Printf("\nTotal: %d\n", result.Total)
		} else {
			col1Width, col2Width, col3Width := 70, 50, 10
			sep := "+" + strings.Repeat("-", col1Width+2) + "+" + strings.Repeat("-", col2Width+2) + "+" + strings.Repeat("-", col3Width+2) + "+"
			fmt.Println(sep)
			fmt.Printf("| %-70s | %-50s | %-10s |\n", "CONTENT", "PATH", "SCORE")
			fmt.Println(sep)
			for i, sr := range results {
				content := strings.Join(strings.Fields(sr.Content), " ")
				if len(content) > 70 {
					content = content[:67] + "..."
				}
				displayPath := sr.Path
				if len(displayPath) > 50 {
					displayPath = displayPath[:47] + "..."
				}
				scoreStr := "-"
				if sr.Score > 0 {
					scoreStr = fmt.Sprintf("%.4f", sr.Score)
				}
				fmt.Printf("| %-70s | %-50s | %-10s |\n", content, displayPath, scoreStr)
				if i >= 99 {
					fmt.Printf("\n... and %d more results\n", result.Total-i-1)
					break
				}
			}
			fmt.Println(sep)
			fmt.Printf("Total: %d\n", result.Total)
		}
	}
}

// isBinaryContent reports whether content looks binary (has null bytes or is
// not valid UTF-8), in which case cat refuses to print it.
func isBinaryContent(content []byte) bool {
	for _, b := range content {
		if b == 0 {
			return true
		}
	}
	return !utf8.Valid(content)
}

// SearchCommandOptions holds parsed `search` options.
type SearchCommandOptions struct {
	Query     string
	TopK      int
	Threshold float64
	Dirs      []string
}

// ListCommandOptions holds parsed `ls` options.
type ListCommandOptions struct {
	Path  string
	Limit int
}

// parseSearchCommandArgs parses: search [-d dir]... -q query [-k top_k] [-t threshold]
// A nil result with nil error means help was printed.
func parseSearchCommandArgs(args []string) (*SearchCommandOptions, error) {
	opts := &SearchCommandOptions{
		TopK:      10,
		Threshold: 0.2,
		Dirs:      []string{},
	}

	for _, arg := range args {
		if arg == "-h" || arg == "--help" {
			printSearchHelp()
			return nil, nil
		}
	}

	i := 0
	for i < len(args) {
		arg := args[i]
		switch arg {
		case "-d", "--dir":
			if i+1 >= len(args) {
				return nil, fmt.Errorf("missing value for %s flag", arg)
			}
			opts.Dirs = append(opts.Dirs, args[i+1])
			i += 2
		case "-q", "--query":
			if i+1 >= len(args) {
				return nil, fmt.Errorf("missing value for %s flag", arg)
			}
			opts.Query = args[i+1]
			i += 2
		case "-k", "--top-k":
			if i+1 >= len(args) {
				return nil, fmt.Errorf("missing value for %s flag", arg)
			}
			topK, err := strconv.Atoi(args[i+1])
			if err != nil {
				return nil, fmt.Errorf("invalid top-k value: %s", args[i+1])
			}
			opts.TopK = topK
			i += 2
		case "-t", "--threshold":
			if i+1 >= len(args) {
				return nil, fmt.Errorf("missing value for %s flag", arg)
			}
			threshold, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return nil, fmt.Errorf("invalid threshold value: %s", args[i+1])
			}
			opts.Threshold = threshold
			i += 2
		default:
			if !strings.HasPrefix(arg, "-") {
				// Backwards-compatible positional handling: a lone trailing token
				// is treated as the query.
				if opts.Query == "" && i == len(args)-1 {
					opts.Query = arg
				} else if opts.Query == "" && i < len(args)-1 {
					// Old "search [path] query" form: first token is a path.
					opts.Dirs = append(opts.Dirs, arg)
					queryParts := []string{}
					for _, part := range args[i+1:] {
						if !strings.HasPrefix(part, "-") {
							queryParts = append(queryParts, part)
						}
					}
					opts.Query = strings.Join(queryParts, " ")
					i = len(args)
					continue
				}
				i++
			} else {
				return nil, fmt.Errorf("unknown flag: %s", arg)
			}
		}
	}

	if opts.Query == "" {
		return nil, fmt.Errorf("query is required (use -q or --query)")
	}
	if len(opts.Dirs) == 0 {
		opts.Dirs = []string{"datasets"}
	}

	return opts, nil
}

// printSearchHelp prints help for the search command.
func printSearchHelp() {
	fmt.Println(`Search command usage: search [options]

Semantic search in datasets.

Options:
  -d, --dir <path>       Directory to search in (repeatable, e.g. -d datasets/kb1)
  -q, --query <query>    Search query (required)
  -k, --top-k <number>   Number of top results to return (default: 10)
  -t, --threshold <num>  Similarity threshold, 0.0-1.0 (default: 0.2)
  -h, --help             Show this help message

Output defaults to JSON. Use \format plain or \format table to change it.

Examples:
  search -d datasets/kb1 -q "neural networks"
  search -q "data mining"
  search -q "RAG" -k 20 -t 0.5`)
}

// printListHelp prints help for the ls command.
func printListHelp() {
	fmt.Println(`List command usage: ls [path] [options]

List contents of a path in the context filesystem.

Arguments:
  [path]                 Path to list (default: root - all providers and folders)
                         Examples: datasets, datasets/kb1, myfolder

Options:
  -n, --limit <number>   Maximum number of items to display (default: 10)
  -h, --help             Show this help message

Examples:
  ls                          # List root (providers and file_manager folders)
  ls datasets                 # List all datasets
  ls datasets/kb1             # List documents in dataset kb1
  ls -n 5                     # List 5 items at root`)
}

// parseListCommandArgs parses: ls [path] [-n limit]
// A nil result with nil error means help was printed.
func parseListCommandArgs(args []string) (*ListCommandOptions, error) {
	opts := &ListCommandOptions{
		Path:  "", // empty path lists the root (providers and file_manager folders)
		Limit: 10,
	}

	for _, arg := range args {
		if arg == "-h" || arg == "--help" {
			printListHelp()
			return nil, nil
		}
	}

	i := 0
	for i < len(args) {
		arg := args[i]
		switch arg {
		case "-n", "--limit":
			if i+1 >= len(args) {
				return nil, fmt.Errorf("missing value for %s flag", arg)
			}
			limit, err := strconv.Atoi(args[i+1])
			if err != nil {
				return nil, fmt.Errorf("invalid limit value: %s", args[i+1])
			}
			opts.Limit = limit
			i += 2
		default:
			if !strings.HasPrefix(arg, "-") {
				opts.Path = arg
			} else {
				return nil, fmt.Errorf("unknown flag: %s", arg)
			}
			i++
		}
	}

	return opts, nil
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
  LIST PROVIDERS;                                        - List configured LLM providers
  LIST AVAILABLE PROVIDERS;                              - List available LLM providers
  LIST INSTANCES FROM PROVIDER 'name';                   - List provider instances
  LIST MODELS FROM 'provider' 'instance';                - List models of a provider instance
  SHOW PROVIDER 'name';                                  - Show provider details
  SHOW CURRENT MODEL;                                    - Show current model settings
  SHOW INSTANCE 'name' FROM PROVIDER 'provider';         - Show provider instance details
  ADD PROVIDER 'name';                                   - Add a provider without API key
  ADD PROVIDER 'name' 'api_key';                         - Add a provider with API key
  DELETE PROVIDER 'name';                                - Delete a provider
  ALTER PROVIDER 'name' NAME 'new_name';                 - Rename a provider (server-side pending)
  CREATE PROVIDER 'name' INSTANCE 'instance' 'api_key';  - Create a provider instance
  ALTER INSTANCE 'name' NAME 'new' FROM PROVIDER 'p';    - Rename a provider instance
  DROP INSTANCE 'name' FROM PROVIDER 'provider';         - Delete a provider instance
  ENABLE MODEL 'model' FROM 'provider' 'instance';       - Enable a model on an instance
  DISABLE MODEL 'model' FROM 'provider' 'instance';      - Disable a model on an instance
  USE MODEL 'provider/instance/model';                   - Set current model for chat
  CHAT 'message';                                        - Chat using current model
  CHAT 'provider/instance/model' 'message';              - Chat with specified model
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

Context Engine Commands (no quotes, no semicolon):
  ls [path] [-n limit]          - List datasets/files (default: root listing)
                                  e.g. ls, ls datasets, ls datasets/kb1, ls myfolder
  search -q <query> [-d dir]    - Semantic search in datasets
        [-k top_k] [-t thresh]    e.g. search -d datasets/kb1 -q "neural networks"
  cat <path>                    - Show a text file's content (e.g. cat myfolder/a.md)
  Use 'ls -h' or 'search -h' for detailed options.

For more information, see documentation.
`
	fmt.Println(help)
}

// Cleanup performs cleanup before exit, restoring the terminal state.
func (c *CLI) Cleanup() {
	if c.line != nil {
		c.line.Close()
	}
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
	// Restore terminal state on exit; the liner was created in NewCLIWithArgs.
	defer c.Cleanup()
	return c.execute(command)
}

// verifyPassword prompts for the password and verifies it, allowing up to 3
// attempts. Used when a username is supplied without a password at startup.
func (c *CLI) verifyPassword() error {
	const maxAttempts = 3
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		var input string
		var err error

		if term.IsTerminal(int(os.Stdin.Fd())) {
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
