package main

import (
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"multirag/internal/cli"
	"multirag/internal/common"
)

func main() { os.Exit(runCLI()) }

func runCLI() int {
	defer common.Sync()
	if err := common.Init("error"); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	// Parse command line arguments (skip program name).
	args, err := cli.ParseConnectionArgs(os.Args[1:])
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		return 1
	}

	// Show help and exit.
	if args.ShowHelp {
		cli.PrintUsage()
		return 0
	}

	// Create CLI instance with parsed arguments.
	cliApp, err := cli.NewCLIWithArgs(args)
	if err != nil {
		fmt.Printf("Failed to create CLI: %v\n", err)
		return 1
	}

	// Handle interrupt signal.
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigChan
		cliApp.Cleanup()
		common.Sync()
		os.Exit(0)
	}()

	// Single command mode when a command was supplied, otherwise interactive.
	if args.Command != "" {
		if err = cliApp.RunSingleCommand(args.Command); err != nil {
			fmt.Printf("Error: %v\n", err)
			return 1
		}
	} else {
		if err = cliApp.Run(); err != nil {
			fmt.Printf("CLI error: %v\n", err)
			return 1
		}
	}
	return 0
}
