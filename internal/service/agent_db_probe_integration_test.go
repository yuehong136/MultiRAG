//go:build integration

package service

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/testsupport"
)

// The real MySQL connector talks to a registered controlled wire server. Only
// DNS policy resolution is replaced here; production policy is tested separately.
func TestAfter10bbeDBProbeMySQLProtocol(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTER10BBE_G_CONFIG")
	var cfg struct {
		Listen string `json:"mysql_listen"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil || cfg.Listen != "127.0.0.1:18962" || os.Getenv("REQUIRE_SERVICES") != "1" {
		t.Fatal("registered controlled probe endpoint required")
	}
	for _, scenario := range []string{"success", "query-error", "bad-result", "cancel-query", "cancel-handshake"} {
		t.Run(scenario, func(t *testing.T) {
			j := testsupport.NewCase(t)
			j.Event("reserve-controlled-mysql-listener-before-listen", map[string]any{"address": cfg.Listen, "network": "tcp4", "scenario": scenario, "statements": []string{"SELECT 1"}, "pid": os.Getpid()})
			listener, err := net.Listen("tcp4", cfg.Listen)
			if err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() {
				conn, err := listener.Accept()
				if err != nil {
					done <- err
					return
				}
				defer conn.Close()
				conn.SetDeadline(time.Now().Add(3 * time.Second))
				if scenario == "cancel-handshake" {
					_, err := readProbePacket(conn)
					if !errors.Is(err, io.EOF) {
						done <- fmt.Errorf("handshake socket not released: %v", err)
						return
					}
					done <- nil
					return
				}
				if err = writeProbePacket(conn, 0, probeGreeting()); err != nil {
					done <- err
					return
				}
				if _, err = readProbePacket(conn); err != nil {
					done <- err
					return
				}
				if err = writeProbePacket(conn, 2, []byte{0, 0, 0, 2, 0, 0, 0}); err != nil {
					done <- err
					return
				}
				query, err := readProbePacket(conn)
				if err != nil {
					done <- err
					return
				}
				if len(query) == 0 || query[0] != 3 || string(query[1:]) != "SELECT 1" {
					done <- errors.New("probe sent an unexpected statement")
					return
				}
				if scenario == "cancel-query" {
					_, err := readProbePacket(conn)
					if !errors.Is(err, io.EOF) {
						done <- fmt.Errorf("query socket not released: %v", err)
						return
					}
					done <- nil
					return
				}
				if scenario == "query-error" {
					err = writeProbePacket(conn, 1, append([]byte{0xff, 0x15, 0x04, '#', 'H', 'Y', '0', '0', '0'}, []byte("private-driver-marker")...))
				} else {
					value := byte('1')
					if scenario == "bad-result" {
						value = '2'
					}
					column := []byte{3, 'd', 'e', 'f', 0, 0, 0, 1, '1', 0, 12, 63, 0, 1, 0, 0, 0, 3, 0, 0, 0, 0, 0}
					for index, payload := range [][]byte{{1}, column, {0xfe, 0, 0, 2, 0}, {1, value}, {0xfe, 0, 0, 2, 0}} {
						if err = writeProbePacket(conn, byte(index+1), payload); err != nil {
							break
						}
					}
				}
				if err != nil {
					done <- err
					return
				}
				quit, err := readProbePacket(conn)
				if err != nil || len(quit) != 1 || quit[0] != 1 {
					done <- errors.New("MySQL connection was not closed cleanly")
					return
				}
				done <- nil
			}()
			t.Cleanup(func() {
				listener.Close()
				select {
				case err := <-done:
					if err != nil {
						t.Error(err)
					}
				case <-time.After(4 * time.Second):
					t.Error("controlled mysql goroutine survived")
				}
				conn, err := net.DialTimeout("tcp4", cfg.Listen, time.Second)
				if conn != nil {
					conn.Close()
				}
				j.Event("controlled-mysql-listener-cleanup-readback", map[string]any{"address": cfg.Listen, "absent": err != nil})
				if err == nil {
					t.Error("controlled mysql listener survived")
				}
			})
			req := probeRequest()
			_, port, err := net.SplitHostPort(cfg.Listen)
			if err != nil {
				t.Fatal(err)
			}
			req.Port = json.RawMessage(port)
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			if strings.HasPrefix(scenario, "cancel-") {
				cancel()
				ctx, cancel = context.WithTimeout(t.Context(), 100*time.Millisecond)
				defer cancel()
			}
			code, err := probeDatabase(ctx, req, func(context.Context, string) (string, string, error) { return "database.invalid", "127.0.0.1", nil }, openDatabaseProbe)
			if scenario == "success" {
				if err != nil || code != common.CodeSuccess {
					t.Fatal(code, err)
				}
			} else if err == nil || code != common.CodeExceptionError || strings.Contains(err.Error(), "private-driver-marker") {
				t.Fatal(code, err)
			}
			j.Event("real-mysql-driver-probe-result", map[string]any{"scenario": scenario, "code": code, "fixed_select_only": true, "safe_error": true})
		})
	}
}

func readProbePacket(conn net.Conn) ([]byte, error) {
	header := make([]byte, 4)
	if _, err := io.ReadFull(conn, header); err != nil {
		return nil, err
	}
	size := int(header[0]) | int(header[1])<<8 | int(header[2])<<16
	if size > 4096 {
		return nil, errors.New("oversized controlled packet")
	}
	body := make([]byte, size)
	_, err := io.ReadFull(conn, body)
	return body, err
}
func writeProbePacket(conn net.Conn, seq byte, body []byte) error {
	packet := append([]byte{byte(len(body)), byte(len(body) >> 8), byte(len(body) >> 16), seq}, body...)
	_, err := conn.Write(packet)
	return err
}
func probeGreeting() []byte {
	capabilities := uint32(1 | 4 | 8 | 512 | 32768 | 524288)
	packet := append([]byte{10}, []byte("8.0.0-controlled\x00")...)
	packet = binary.LittleEndian.AppendUint32(packet, 1)
	packet = append(packet, []byte("12345678")...)
	packet = append(packet, 0)
	packet = binary.LittleEndian.AppendUint16(packet, uint16(capabilities))
	packet = append(packet, 45)
	packet = binary.LittleEndian.AppendUint16(packet, 2)
	packet = binary.LittleEndian.AppendUint16(packet, uint16(capabilities>>16))
	packet = append(packet, 21)
	packet = append(packet, make([]byte, 10)...)
	packet = append(packet, []byte("abcdefghijkl\x00mysql_native_password\x00")...)
	return packet
}
