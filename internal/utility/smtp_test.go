package utility

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/testsupport"
)

func TestCanonicalSMTPDeliveryCancellationAndAcceptedProof(t *testing.T) {
	smtp := testsupport.NewSMTP(t)
	host, port := smtp.HostPort()
	config := common.SMTPConfig{MailServer: host, MailPort: port, MailFromAddress: "sender@example.invalid"}
	if err := SendResetCodeEmail(t.Context(), config, "member@example.invalid", "ABCD", 5); err != nil {
		t.Fatal(err)
	}
	if messages := smtp.Messages(); len(messages) != 1 || !strings.Contains(messages[0], "ABCD") {
		t.Fatal("real SMTP did not accept message")
	}
	entered, release := smtp.PauseData()
	defer release()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- SendResetCodeEmail(ctx, config, "member@example.invalid", "EFGH", 5) }()
	<-entered
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("SMTP cancellation identity lost", err)
		}
	case <-time.After(time.Second):
		t.Fatal("SMTP socket ignored cancellation")
	}
	release()
}
