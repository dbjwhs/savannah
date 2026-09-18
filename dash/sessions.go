// sessions.go: the LOCAL SESSIONS half of the board.
//
// The tab-hell fix. Every interactive Claude Code session on this machine
// writes its live state to a file via the session-status hook (see
// hooks/session-status.py); the dash reads that directory and shows every
// open tab on one board, floating the ones that need you to the top and
// counting them in the header. Enter on a session row jumps you to its
// terminal tab (iTerm2, or tmux).
//
// This half needs no savannahd, no mesh, no network: it is pure local file
// reads, so `savannah-dash --no-mesh` is a standalone tab monitor for a
// locked-down machine. Everything is best-effort: a session whose hook could
// not capture a terminal id simply will not jump.

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

type jumpMsg struct {
	id  string
	ok  bool
	how string
}
type dismissMsg struct {
	id string
	ok bool
}

// session mirrors one hook-written status file (hooks/session-status.py).
type session struct {
	ID      string `json:"session_id"`
	Project string `json:"project"`
	State   string `json:"state"` // working | waiting | idle | done
	Message string `json:"message"`
	Notify  string `json:"notify_type"` // permission_prompt | idle_prompt | ""
	Mode    string `json:"mode"`
	ITerm   string `json:"iterm_session_id"`
	Term    string `json:"term"`
	Tmux    string `json:"tmux_pane"`
	Updated int64  `json:"updated"`
	PID     int    `json:"pid"`
	file    string // source path, for dismiss
}

type sessionsMsg struct {
	sessions []session
	err      string
}

func sessionsDir() string {
	if d := os.Getenv("SAVANNAH_SESSIONS_DIR"); d != "" {
		return d
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".claude", "savannah", "sessions")
}

// readSessions loads every status file in dir, sorted for display. A missing
// directory is not an error (no sessions yet); an unparseable file is skipped.
func readSessions(dir string) ([]session, error) {
	if dir == "" {
		return nil, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out []session
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		p := filepath.Join(dir, e.Name())
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var s session
		if json.Unmarshal(b, &s) != nil || s.ID == "" {
			continue
		}
		s.file = p
		out = append(out, s)
	}
	sortSessions(out)
	return out, nil
}

// attentionRank orders states for display: what needs you first.
func attentionRank(state string) int {
	switch state {
	case "waiting":
		return 0
	case "working":
		return 1
	case "done":
		return 2
	default: // idle, unknown
		return 3
	}
}

// sortSessions floats attention to the top. Within "waiting", the one you
// have left waiting longest (oldest update) comes first; within every other
// bucket, most recently active first.
func sortSessions(s []session) {
	sort.SliceStable(s, func(i, j int) bool {
		ri, rj := attentionRank(s[i].State), attentionRank(s[j].State)
		if ri != rj {
			return ri < rj
		}
		if s[i].State == "waiting" {
			return s[i].Updated < s[j].Updated // longest wait on top
		}
		return s[i].Updated > s[j].Updated // freshest on top
	})
}

func sessionAttention(s session) bool { return s.State == "waiting" }

func countSessionAttention(ss []session) int {
	n := 0
	for _, s := range ss {
		if sessionAttention(s) {
			n++
		}
	}
	return n
}

// humanAge renders a seconds delta compactly: 5s, 3m, 2h, 4d.
func humanAge(sec int64) string {
	if sec < 0 {
		sec = 0
	}
	switch {
	case sec < 60:
		return fmt.Sprintf("%ds", sec)
	case sec < 3600:
		return fmt.Sprintf("%dm", sec/60)
	case sec < 86400:
		return fmt.Sprintf("%dh", sec/3600)
	default:
		return fmt.Sprintf("%dd", sec/86400)
	}
}

// projectLabel shortens a cwd to its last two path components.
func projectLabel(path string) string {
	path = strings.TrimRight(path, "/")
	if path == "" {
		return "~"
	}
	parts := strings.Split(path, "/")
	nonEmpty := parts[:0]
	for _, p := range parts {
		if p != "" {
			nonEmpty = append(nonEmpty, p)
		}
	}
	if len(nonEmpty) == 0 {
		return "/"
	}
	if len(nonEmpty) <= 2 {
		return strings.Join(nonEmpty, "/")
	}
	return strings.Join(nonEmpty[len(nonEmpty)-2:], "/")
}

// stateGlyph is a one-rune status marker for a session row.
func stateGlyph(state string) string {
	switch state {
	case "waiting":
		return "●"
	case "working":
		return "…"
	case "done":
		return "✓"
	default:
		return "·"
	}
}

// sessionDetail is the right-hand cell: the reason it needs you, or a mode
// hint, or the state word.
func sessionDetail(s session) string {
	if s.State == "waiting" {
		switch s.Notify {
		case "permission_prompt":
			if s.Message != "" {
				return "needs permission: " + s.Message
			}
			return "needs permission"
		case "idle_prompt":
			return "idle, waiting for you"
		}
		if s.Message != "" {
			return s.Message
		}
		return "needs you"
	}
	if s.Mode != "" && s.Mode != "default" {
		return s.State + " (" + s.Mode + ")"
	}
	return s.State
}

// jumpSessionCmd focuses a session's terminal tab. iTerm2 first (the id after
// the colon in ITERM_SESSION_ID is the session's AppleScript id), then tmux.
// Best-effort: no known terminal is a no-op with a status hint, not an error.
func jumpSessionCmd(s session) tea.Cmd {
	return func() tea.Msg {
		if s.ITerm != "" {
			uuid := s.ITerm
			if i := strings.IndexByte(uuid, ':'); i >= 0 {
				uuid = uuid[i+1:]
			}
			// Return whether the tab was actually found, so a stale id reads
			// as "tab not found" instead of a false "jumped". Only activate
			// iTerm when found, so a miss does not yank it forward.
			script := fmt.Sprintf(`set found to false
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is %q then
          select w
          tell t to select
          select s
          set found to true
        end if
      end repeat
    end repeat
  end repeat
  if found then activate
end tell
return found`, uuid)
			out, err := exec.Command("osascript", "-e", script).Output()
			found := err == nil && strings.HasPrefix(strings.TrimSpace(string(out)), "true")
			how := "iTerm"
			if err == nil && !found {
				how = "iTerm: tab not found"
			}
			return jumpMsg{id: s.ID, ok: found, how: how}
		}
		if s.Tmux != "" {
			// Select the pane and bring its window forward in the attached client.
			err := exec.Command("tmux", "select-pane", "-t", s.Tmux).Run()
			if err == nil {
				exec.Command("tmux", "select-window", "-t", s.Tmux).Run()
			}
			return jumpMsg{id: s.ID, ok: err == nil, how: "tmux"}
		}
		return jumpMsg{id: s.ID, ok: false, how: "no terminal id"}
	}
}

// dismissSessionCmd deletes a stale session file (a crashed session that
// never fired SessionEnd). Live sessions rewrite it on their next event.
func dismissSessionCmd(s session) tea.Cmd {
	return func() tea.Msg {
		var ok bool
		if s.file != "" {
			ok = os.Remove(s.file) == nil
		}
		return dismissMsg{id: s.ID, ok: ok}
	}
}

func readSessionsCmd() tea.Cmd {
	dir := sessionsDir()
	return func() tea.Msg {
		ss, err := readSessions(dir)
		if err != nil {
			return sessionsMsg{err: err.Error()}
		}
		return sessionsMsg{sessions: ss}
	}
}

// nowUnix is time.Now().Unix(), pulled out so View can stamp every row age
// against one instant.
func nowUnix() int64 { return time.Now().Unix() }
