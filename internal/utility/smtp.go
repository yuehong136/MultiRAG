package utility

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"net/mail"
	"net/smtp"
	"strconv"
	"strings"
	"time"

	"multirag/internal/common"
)

func SendResetCodeEmail(ctx context.Context, cfg common.SMTPConfig, toEmail, otp string, ttlMinutes int) error {
	if cfg.MailServer == "" || cfg.MailPort < 1 || cfg.MailPort > 65535 {
		return fmt.Errorf("smtp is not configured")
	}
	if cfg.MailUseSSL && cfg.MailUseTLS {
		return fmt.Errorf("choose one smtp TLS mode")
	}
	if cfg.MailUsername != "" && !cfg.MailUseSSL && !cfg.MailUseTLS {
		return fmt.Errorf("smtp authentication requires TLS")
	}
	from := cfg.MailFromAddress
	if from == "" {
		from = cfg.MailUsername
	}
	fromAddress, err := mail.ParseAddress(from)
	if err != nil || fromAddress.Address != from || strings.ContainsAny(from+toEmail+cfg.MailFromName, "\r\n") {
		return fmt.Errorf("invalid smtp address")
	}
	to, err := mail.ParseAddress(toEmail)
	if err != nil || to.Address != toEmail {
		return fmt.Errorf("invalid recipient")
	}
	seconds := cfg.TimeoutSeconds
	if seconds == 0 {
		seconds = 10
	}
	if seconds < 1 || seconds > 120 {
		return fmt.Errorf("invalid smtp timeout")
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(seconds)*time.Second)
	defer cancel()
	address := net.JoinHostPort(cfg.MailServer, strconv.Itoa(cfg.MailPort))
	dialer := &net.Dialer{Timeout: time.Duration(seconds) * time.Second}
	var connection net.Conn
	tlsConfig := &tls.Config{ServerName: cfg.MailServer, MinVersion: tls.VersionTLS12}
	if cfg.MailUseSSL {
		connection, err = (&tls.Dialer{NetDialer: dialer, Config: tlsConfig}).DialContext(ctx, "tcp", address)
	} else {
		connection, err = dialer.DialContext(ctx, "tcp", address)
	}
	if err != nil {
		return err
	}
	defer connection.Close()
	deadline, _ := ctx.Deadline()
	if err = connection.SetDeadline(deadline); err != nil {
		return err
	}
	stop := context.AfterFunc(ctx, func() { connection.Close() })
	defer stop()
	client, err := smtp.NewClient(connection, cfg.MailServer)
	if err != nil {
		return err
	}
	defer client.Close()
	if cfg.MailUseTLS {
		if ok, _ := client.Extension("STARTTLS"); !ok {
			return fmt.Errorf("smtp STARTTLS is unavailable")
		}
		if err = client.StartTLS(tlsConfig); err != nil {
			return err
		}
	}
	if cfg.MailUsername != "" {
		if err = client.Auth(smtp.PlainAuth("", cfg.MailUsername, cfg.MailPassword, cfg.MailServer)); err != nil {
			return err
		}
	}
	if err = client.Mail(from); err != nil {
		return err
	}
	if err = client.Rcpt(toEmail); err != nil {
		return err
	}
	writer, err := client.Data()
	if err != nil {
		return err
	}
	name := cfg.MailFromName
	if name == "" {
		name = "MultiRAG"
	}
	sender := (&mail.Address{Name: name, Address: from}).String()
	message := "From: " + sender + "\r\nTo: " + toEmail + "\r\nSubject: Your Password Reset Code\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour password reset code is: " + otp + "\r\nThis code expires in " + strconv.Itoa(ttlMinutes) + " minutes.\r\n"
	if _, err = writer.Write([]byte(message)); err != nil {
		writer.Close()
		return err
	}
	if err = writer.Close(); err != nil {
		return err
	}
	// DATA's 250 reply is the delivery acknowledgement. A failed QUIT cannot
	// retract an already accepted message; the connection still closes above.
	_ = client.Quit()
	return nil
}
