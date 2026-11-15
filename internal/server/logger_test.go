package server

import "testing"

func TestMissingOptionalConfig(t *testing.T) {
	previous, viper := globalConfig, globalViper
	t.Cleanup(func() { globalConfig, globalViper = previous, viper })
	t.Chdir(t.TempDir())
	if err := FromConfigFile(""); err != nil {
		t.Fatal(err)
	}
}
