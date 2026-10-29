// sessions.go: the LOCAL SESSIONS half of the board.
//
// The tab-hell fix. Every interactive Claude Code session on this machine
// writes its live state to a file via the session-status hook (see
// hooks/session-status.py); the dash reads that directory and shows every
// open tab on one board, floating the ones that need you to the top and
// counting them in the header. Enter on a session row jumps you to its
// terminal tab (iTerm2, or tmux).
//
// Work sessions (work/savannah_work.py, Phase 5d) ride the same half. A work
// session is a file-owned record (<work dir>/<name>/record.json) naming the
// git worktree its interactive Claude tab runs in. The dash joins each record
// to the hook session whose project is that worktree, at read time, and shows
// the name in a WORK column. A work session with no live hook session still
// gets a row (state "offline") so you can see it and jump to its pane; the
// WHAT cell then carries the first line of its last note.
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
	Work    string `json:"-"` // joined work-session name, "" when none
	file    string // source path, for dismiss
}

// workRecord mirrors one <work dir>/<name>/record.json written by
// work/savannah_work.py. The dash only reads these; `work rm` owns deletion.
type workRecord struct {
	Name       string `json:"name"`
	Repo       string `json:"repo"`
	Worktree   string `json:"worktree"`
	Branch     string `json:"branch"`
	Created    int64  `json:"created"`
	Updated    int64  `json:"updated"`
	Terminal   string `json:"terminal"`
	TabID      string `json:"tab_id"`
	ClaudePane string `json:"claude_pane"`
	ShellPane  string `json:"shell_pane"`
	Prompt     string `json:"prompt"`
	lastNote   string // first line of the last note in notes.md, "" if none
}

// workRowPrefix marks a synthesized row for a work session with no live hook
// session: its ID is "work:<name>", which no hook file can produce.
const workRowPrefix = "work:"

// sessionsMsg carries the two inputs separately; the model joins them. A read
// error on one side leaves that side's field nil and names the error, and the
// model keeps its last good list for that side instead of wiping rows.
type sessionsMsg struct {
	hooks   []session    // hook sessions (readSessions), nil on err
	work    []workRecord // work records (readWork), nil on workErr
	err     string       // sessions-dir read error
	workErr string       // work-dir read error
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

func workDir() string {
	if d := os.Getenv("SAVANNAH_WORK_DIR"); d != "" {
		return d
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".claude", "savannah", "work")
}

// validWorkName is the work-session name rule, mirrored from
// work/savannah_work.py (valid_name) so the dash and the CLI list the same
// directories: ^[a-z0-9][a-z0-9._-]{0,63}$, never a mesh task id (t-<digits>),
// never "archive" (the notes graveyard), never ".." anywhere, never a trailing
// ".", never a ".lock" suffix (git refs). Change both sides together.
func validWorkName(name string) bool {
	if name == "" || len(name) > 64 || name == "archive" || strings.Contains(name, "..") {
		return false
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9':
		case i > 0 && (c == '.' || c == '_' || c == '-'):
		default:
			return false
		}
	}
	if strings.HasSuffix(name, ".") || strings.HasSuffix(name, ".lock") {
		return false
	}
	if strings.HasPrefix(name, "t-") && len(name) > 2 {
		digits := true
		for i := 2; i < len(name); i++ {
			if name[i] < '0' || name[i] > '9' {
				digits = false
				break
			}
		}
		if digits {
			return false
		}
	}
	return true
}

// readWork loads every <dir>/<name>/record.json. A missing directory is not
// an error (no work sessions yet); the archive/ folder, any directory whose
// name fails validWorkName (the CLI would not list it either), any
// unparseable record, and any record without a worktree are skipped. The first line of the last note is attached
// when notes.md exists.
func readWork(dir string) ([]workRecord, error) {
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
	var out []workRecord
	for _, e := range entries {
		if !e.IsDir() || e.Name() == "archive" || !validWorkName(e.Name()) {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, e.Name(), "record.json"))
		if err != nil {
			continue
		}
		var r workRecord
		if json.Unmarshal(b, &r) != nil {
			continue
		}
		// The directory is the name, never the file (the CLI does the same, so
		// `work rm <name>` always addresses what the board shows). A record
		// without a worktree cannot be joined or removed sanely; the CLI skips
		// it too.
		r.Name = e.Name()
		if r.Worktree == "" {
			continue
		}
		if nb, err := os.ReadFile(filepath.Join(dir, e.Name(), "notes.md")); err == nil {
			r.lastNote = lastNoteFirstLine(string(nb))
		}
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// lastNoteFirstLine returns the first non-blank line of the last note in a
// notes.md written by `work note` (entries start with a "## <stamp>" heading).
// A file with no heading yields its first non-blank line; empty yields "".
func lastNoteFirstLine(notes string) string {
	lines := strings.Split(notes, "\n")
	start := 0
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.HasPrefix(lines[i], "## ") {
			start = i + 1
			break
		}
	}
	for _, ln := range lines[start:] {
		if t := strings.TrimSpace(ln); t != "" {
			return t
		}
	}
	return ""
}

// samePath is the join rule: realpath(a) == realpath(b). When either side
// cannot be resolved (a worktree already removed, say), fall back to a
// cleaned lexical compare so a stale but identical path still joins.
func samePath(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	ra, errA := filepath.EvalSymlinks(a)
	rb, errB := filepath.EvalSymlinks(b)
	if errA == nil && errB == nil {
		return ra == rb
	}
	return filepath.Clean(a) == filepath.Clean(b)
}

// itermUUID strips the "w0t0p0:" prefix from an ITERM_SESSION_ID, leaving the
// AppleScript session id, which is the form record.claude_pane uses.
func itermUUID(id string) string {
	if i := strings.IndexByte(id, ':'); i >= 0 {
		return id[i+1:]
	}
	return id
}

// joinCandidate reports whether sessions[i] may be joined to rec: an unclaimed
// hook session (never a synthesized work row) whose project is the record's
// worktree.
func joinCandidate(rec workRecord, sessions []session, claimed []bool, i int) bool {
	if claimed != nil && claimed[i] {
		return false
	}
	s := sessions[i]
	return !isWorkRow(s) && samePath(s.Project, rec.Worktree)
}

// matchPane finds the unclaimed candidate running in the record's Claude
// pane. Returns the index into sessions, or -1 (also when the record has no
// pane: an empty pane never matches an empty ITerm by accident).
func matchPane(rec workRecord, sessions []session, claimed []bool) int {
	if rec.ClaudePane == "" {
		return -1
	}
	for i, s := range sessions {
		if joinCandidate(rec, sessions, claimed, i) && itermUUID(s.ITerm) == rec.ClaudePane {
			return i
		}
	}
	return -1
}

// matchNewest finds the most recently updated unclaimed candidate. Returns
// the index into sessions, or -1.
func matchNewest(rec workRecord, sessions []session, claimed []bool) int {
	best := -1
	for i, s := range sessions {
		if !joinCandidate(rec, sessions, claimed, i) {
			continue
		}
		if best < 0 || s.Updated > sessions[best].Updated {
			best = i
		}
	}
	return best
}

// pickWorkSession finds the hook session a single work record belongs to: the
// one whose project is the record's worktree. With several candidates, prefer
// the one running in the record's Claude pane, else the most recently updated.
// Returns the index into sessions, or -1. joinWork does the same over many
// records with claims, so one session is never tagged by two records.
func pickWorkSession(rec workRecord, sessions []session) int {
	if i := matchPane(rec, sessions, nil); i >= 0 {
		return i
	}
	return matchNewest(rec, sessions, nil)
}

// offlineWorkRow synthesizes the board row for a work session with no live
// hook session. ITerm carries the Claude pane id so jumpSessionCmd focuses it
// unchanged; Message carries the last note so WHAT says what you were doing.
func offlineWorkRow(rec workRecord) session {
	return session{
		ID:      workRowPrefix + rec.Name,
		Project: rec.Worktree,
		State:   "offline",
		Message: rec.lastNote,
		ITerm:   rec.ClaudePane,
		Term:    rec.Terminal,
		Updated: rec.Updated,
		Work:    rec.Name,
	}
}

// isWorkRow reports whether a session is a synthesized offline work row (no
// hook file behind it, so nothing to dismiss; `work rm` owns the record).
func isWorkRow(s session) bool { return strings.HasPrefix(s.ID, workRowPrefix) }

// joinWork tags each hook session with the work session it belongs to and
// appends an offline row for every work record with no live session. The
// result is sorted for display.
//
// Each hook session is tagged by at most one record and each record tags at
// most one session. Two passes: first every record whose Claude pane is live
// claims that session (over all records, so a pane match is never stolen by
// another record's fallback), then the remaining records take the most
// recently updated UNCLAIMED session in their worktree, in record order.
// Offline rows are synthesized after both passes and are never join
// candidates, so when every tab is closed each record gets its own row.
//
// The function is idempotent over its own output: synthesized rows in the
// input are dropped and stale WORK tags are cleared before joining, so
// re-joining a previously joined list against fresh records is safe.
func joinWork(sessions []session, work []workRecord) []session {
	out := make([]session, 0, len(sessions)+len(work))
	for _, s := range sessions {
		if isWorkRow(s) {
			continue
		}
		s.Work = ""
		out = append(out, s)
	}
	claimed := make([]bool, len(out))
	tagged := make([]bool, len(work))
	for r, rec := range work {
		if i := matchPane(rec, out, claimed); i >= 0 {
			out[i].Work = rec.Name
			claimed[i] = true
			tagged[r] = true
		}
	}
	var offline []session
	for r, rec := range work {
		if tagged[r] {
			continue
		}
		if i := matchNewest(rec, out, claimed); i >= 0 {
			out[i].Work = rec.Name
			claimed[i] = true
			continue
		}
		offline = append(offline, offlineWorkRow(rec))
	}
	out = append(out, offline...)
	sortSessions(out)
	return out
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
	case "offline": // a work session with no live tab
		return 4
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

// projectLabel shortens a cwd to its last two path components. A work
// session's worktree (<repo>/.savannah-worktrees/<name>) reads as
// "<repo>/<name>", since the fixed middle component would otherwise eat the
// whole cell.
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
	if n := len(nonEmpty); n >= 3 && nonEmpty[n-2] == ".savannah-worktrees" {
		return nonEmpty[n-3] + "/" + nonEmpty[n-1]
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
	case "offline":
		return "○"
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
	if s.State == "offline" {
		if s.Message != "" {
			return s.Message
		}
		return "offline, no live session"
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
			uuid := itermUUID(s.ITerm)
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

// readSessionsCmd reads hook sessions and work records on one tick and hands
// both to the model, which joins them (see model.applySessions). A failure on
// one side still delivers the other side's rows; the failed side's list is
// nil and its error string set, and the model keeps its last good list.
func readSessionsCmd() tea.Cmd {
	dir := sessionsDir()
	wdir := workDir()
	return func() tea.Msg {
		var msg sessionsMsg
		ss, err := readSessions(dir)
		if err != nil {
			msg.err = err.Error()
		} else {
			msg.hooks = ss
		}
		ws, werr := readWork(wdir)
		if werr != nil {
			msg.workErr = werr.Error()
		} else {
			msg.work = ws
		}
		return msg
	}
}

// nowUnix is time.Now().Unix(), pulled out so View can stamp every row age
// against one instant.
func nowUnix() int64 { return time.Now().Unix() }
