package common

// SMTPConfig is shared by configuration and the transactional email sender.
type SMTPConfig struct {
	MailServer      string `mapstructure:"mail_server"`
	MailPort        int    `mapstructure:"mail_port"`
	MailUseSSL      bool   `mapstructure:"mail_use_ssl"`
	MailUseTLS      bool   `mapstructure:"mail_use_tls"`
	MailUsername    string `mapstructure:"mail_username"`
	MailPassword    string `mapstructure:"mail_password"`
	MailFromName    string `mapstructure:"mail_from_name"`
	MailFromAddress string `mapstructure:"mail_from_address"`
	MailFrontendURL string `mapstructure:"mail_frontend_url"`
	TimeoutSeconds  int    `mapstructure:"timeout_seconds"`
}
