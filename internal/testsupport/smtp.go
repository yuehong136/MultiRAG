package testsupport

import (
	"bufio"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/textproto"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// SMTPServer is an owned loopback protocol boundary. Message bodies stay in
// memory; journals contain listener identities and counts, never OTP values.
type SMTPServer struct {
	listener    net.Listener
	mu          sync.Mutex
	connections map[net.Conn]bool
	messages    []string
	reject      bool
	entered     chan struct{}
	release     chan struct{}
	shutdown    chan struct{}
	wg          sync.WaitGroup
}

func NewSMTP(t testing.TB) *SMTPServer {
	t.Helper()
	j := NewCase(t)
	var listener net.Listener
	address := ""
	for range 30 {
		var value [2]byte
		if _, err := rand.Read(value[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(value[:]))%20000)
		j.Event("reserve-smtp-listener", map[string]any{"address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		j.Event("smtp-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("controlled SMTP listener unavailable")
	}
	s := &SMTPServer{listener: listener, connections: map[net.Conn]bool{}, shutdown: make(chan struct{})}
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			s.mu.Lock()
			select {
			case <-s.shutdown:
				s.mu.Unlock()
				connection.Close()
				return
			default:
			}
			s.connections[connection] = true
			s.wg.Add(1)
			s.mu.Unlock()
			go func() {
				defer s.wg.Done()
				defer connection.Close()
				defer func() { s.mu.Lock(); delete(s.connections, connection); s.mu.Unlock() }()
				s.serve(connection)
			}()
		}
	}()
	t.Cleanup(func() {
		close(s.shutdown)
		listener.Close()
		s.mu.Lock()
		for connection := range s.connections {
			connection.Close()
		}
		s.mu.Unlock()
		done := make(chan struct{})
		go func() { s.wg.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("SMTP handlers survived shutdown")
		}
		connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("SMTP listener survived cleanup")
		}
		j.Event("smtp-listener-cleaned", map[string]any{"address": address, "absent": err != nil, "accepted_messages": len(s.Messages())})
	})
	return s
}

func (s *SMTPServer) HostPort() (string, int) {
	host, port, _ := net.SplitHostPort(s.listener.Addr().String())
	number, _ := strconv.Atoi(port)
	return host, number
}
func (s *SMTPServer) Messages() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.messages...)
}
func (s *SMTPServer) RejectData(reject bool) { s.mu.Lock(); s.reject = reject; s.mu.Unlock() }
func (s *SMTPServer) PauseData() (<-chan struct{}, func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.entered = make(chan struct{})
	s.release = make(chan struct{})
	release := s.release
	var once sync.Once
	return s.entered, func() { once.Do(func() { close(release) }) }
}

func (s *SMTPServer) serve(connection net.Conn) {
	reader := textproto.NewReader(bufio.NewReader(connection))
	writer := bufio.NewWriter(connection)
	reply := func(value string) bool {
		if _, err := writer.WriteString(value + "\r\n"); err != nil {
			return false
		}
		return writer.Flush() == nil
	}
	if !reply("220 controlled SMTP") {
		return
	}
	for {
		line, err := reader.ReadLine()
		if err != nil {
			return
		}
		command := strings.ToUpper(strings.SplitN(line, " ", 2)[0])
		switch command {
		case "EHLO", "HELO":
			if !reply("250-controlled\r\n250 OK") {
				return
			}
		case "MAIL", "RCPT", "RSET", "NOOP":
			if !reply("250 OK") {
				return
			}
		case "DATA":
			if !reply("354 send message") {
				return
			}
			data, err := io.ReadAll(reader.DotReader())
			if err != nil {
				return
			}
			s.mu.Lock()
			entered, release, reject := s.entered, s.release, s.reject
			s.entered, s.release = nil, nil
			s.mu.Unlock()
			if entered != nil {
				close(entered)
				select {
				case <-release:
				case <-s.shutdown:
					return
				}
			}
			if reject {
				if !reply("550 controlled delivery failure") {
					return
				}
			} else {
				s.mu.Lock()
				s.messages = append(s.messages, string(data))
				s.mu.Unlock()
				if !reply("250 accepted") {
					return
				}
			}
		case "QUIT":
			reply("221 bye")
			return
		default:
			if !reply("502 unsupported command") {
				return
			}
		}
	}
}
