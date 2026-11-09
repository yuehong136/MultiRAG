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

package tool

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net"
	"net/mail"
	"net/smtp"
	"strings"
	"time"

	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/schema"
)

const emailToolName = "email"

const emailToolDescription = "Send an email via SMTP. Returns success/failure status."

// emailParams is the JSON shape the model sends into InvokableRun.
type emailParams struct {
	SMTPHost string   `json:"smtp_host"`
	SMTPPort int      `json:"smtp_port"`
	Username string   `json:"username"`
	Password string   `json:"password"`
	FromAddr string   `json:"from_addr"`
	ToAddrs  []string `json:"to_addrs"`
	Subject  string   `json:"subject"`
	Body     string   `json:"body"`
}

// emailEnvelope is what the model sees.
type emailEnvelope struct {
	OK    bool   `json:"ok"`
	Error string `json:"_ERROR,omitempty"`
}

// EmailTool is the Phase 3 batch 3 implementation of the SMTP email
// sender tool (plan §2.11.4 row 7, §5 Phase 3 第 3 批). It composes an
// RFC 822 message and submits it via the stdlib net/smtp client. All
// authentication modes supported by net/smtp.Auth are available
// (PLAIN, LOGIN, CRAM-MD5) by selecting the appropriate creds.
type EmailTool struct{}

// NewEmailTool returns an EmailTool. There is no shared HTTPHelper
// (SMTP is not HTTP), so the constructor is the simplest possible.
func NewEmailTool() *EmailTool {
	return &EmailTool{}
}

// Info returns the tool's metadata for the chat model.
func (e *EmailTool) Info(_ context.Context) (*schema.ToolInfo, error) {
	return &schema.ToolInfo{
		Name: emailToolName,
		Desc: emailToolDescription,
		ParamsOneOf: schema.NewParamsOneOfByParams(map[string]*schema.ParameterInfo{
			"smtp_host": {
				Type:     schema.String,
				Desc:     "SMTP server hostname (e.g. smtp.gmail.com).",
				Required: true,
			},
			"smtp_port": {
				Type:     schema.Integer,
				Desc:     "SMTP server port (e.g. 587 for STARTTLS, 465 for implicit TLS).",
				Required: true,
			},
			"username": {
				Type:     schema.String,
				Desc:     "SMTP authentication username. Empty for unauthenticated relay.",
				Required: false,
			},
			"password": {
				Type:     schema.String,
				Desc:     "SMTP authentication password (or app password for Gmail/Yahoo).",
				Required: false,
			},
			"from_addr": {
				Type:     schema.String,
				Desc:     "Sender email address (RFC 5322).",
				Required: true,
			},
			"to_addrs": {
				Type:     schema.Array,
				Desc:     "Recipient email addresses.",
				Required: true,
			},
			"subject": {
				Type:     schema.String,
				Desc:     "Email subject line.",
				Required: true,
			},
			"body": {
				Type:     schema.String,
				Desc:     "Email body (plain text).",
				Required: true,
			},
		}),
	}, nil
}

// buildEmailMessage composes the RFC 822 wire format: headers + blank
// line + body. Extracted so tests can verify subject / recipient
// inclusion without opening a real socket.
func buildEmailMessage(from string, to []string, subject, body string) []byte {
	var b strings.Builder
	b.WriteString("From: " + from + "\r\n")
	b.WriteString("To: " + strings.Join(to, ", ") + "\r\n")
	b.WriteString("Subject: " + subject + "\r\n")
	b.WriteString("MIME-Version: 1.0\r\n")
	b.WriteString("Content-Type: text/plain; charset=UTF-8\r\n")
	b.WriteString("\r\n")
	b.WriteString(body)
	b.WriteString("\r\n")
	return []byte(b.String())
}

// InvokableRun sends the email. We delegate to smtp.SendMail which
// handles EHLO, STARTTLS, and AUTH transparently when an *smtp.Auth is
// supplied; with nil auth it sends unauthenticated.
func (e *EmailTool) InvokableRun(ctx context.Context, argsJSON string, _ ...tool.Option) (string, error) {
	var p emailParams
	if err := json.Unmarshal([]byte(argsJSON), &p); err != nil {
		return emailErrJSON(fmt.Errorf("email: parse arguments: %w", err)),
			fmt.Errorf("email: parse arguments: %w", err)
	}
	if err := validateEmailParams(&p); err != nil {
		return emailErrJSON(err), err
	}

	addr := fmt.Sprintf("%s:%d", p.SMTPHost, p.SMTPPort)
	msg := buildEmailMessage(p.FromAddr, p.ToAddrs, p.Subject, p.Body)

	var auth smtp.Auth
	if p.Username != "" {
		auth = smtp.PlainAuth("", p.Username, p.Password, p.SMTPHost)
	}

	conn, err := (&net.Dialer{Timeout: 30 * time.Second}).DialContext(ctx, "tcp", addr)
	if err != nil {
		return emailErrJSON(err), err
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	client, err := smtp.NewClient(conn, p.SMTPHost)
	if err != nil {
		if ctx.Err() != nil {
			err = ctx.Err()
		} else if deadline, ok := ctx.Deadline(); ok && !time.Now().Before(deadline) {
			err = context.DeadlineExceeded
		}
		return emailErrJSON(err), err
	}
	defer client.Close()
	if ok, _ := client.Extension("STARTTLS"); ok {
		err = client.StartTLS(&tls.Config{ServerName: p.SMTPHost, MinVersion: tls.VersionTLS12})
	}
	if err == nil && auth != nil {
		err = client.Auth(auth)
	}
	if err == nil {
		err = client.Mail(p.FromAddr)
	}
	for _, recipient := range p.ToAddrs {
		if err == nil {
			err = client.Rcpt(recipient)
		}
	}
	if err == nil {
		writer, werr := client.Data()
		err = werr
		if err == nil {
			_, err = writer.Write(msg)
			cerr := writer.Close()
			if err == nil {
				err = cerr
			}
		}
	}
	if err == nil {
		err = client.Quit()
	}
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	if err != nil {
		return emailErrJSON(err), err
	}

	return emailJSON(emailEnvelope{OK: true}), nil
}

// validateEmailParams guards against obviously broken inputs. The
// upstream SMTP server will give a better error for malformed
// addresses, but the common case (empty / missing) is caught here.
func validateEmailParams(p *emailParams) error {
	switch {
	case p.SMTPHost == "":
		return fmt.Errorf("email: smtp_host is required")
	case p.SMTPPort <= 0 || p.SMTPPort > 65535:
		return fmt.Errorf("email: smtp_port must be in [1, 65535]")
	case p.FromAddr == "":
		return fmt.Errorf("email: from_addr is required")
	case len(p.ToAddrs) == 0:
		return fmt.Errorf("email: to_addrs is required and must be non-empty")
	case p.Subject == "":
		return fmt.Errorf("email: subject is required")
	}
	for _, header := range append([]string{p.FromAddr, p.Subject}, p.ToAddrs...) {
		if strings.ContainsAny(header, "\r\n") {
			return fmt.Errorf("email: header contains newline")
		}
	}
	for _, address := range append([]string{p.FromAddr}, p.ToAddrs...) {
		parsed, err := mail.ParseAddress(address)
		if err != nil || parsed.Address != address {
			return fmt.Errorf("email: invalid envelope address")
		}
	}
	if len(p.Body) > maxToolResponseBytes {
		return fmt.Errorf("email: body exceeds limit")
	}

	return nil
}

func emailJSON(env emailEnvelope) string {
	b, err := json.Marshal(env)
	if err != nil {
		return fmt.Sprintf(`{"_ERROR":"email: marshal result: %s"}`, err)
	}
	return string(b)
}

func emailErrJSON(err error) string {
	return emailJSON(emailEnvelope{Error: err.Error()})
}
