// snapshot.go: the Ctrl-S screen. Save every session's state before a
// reboot; bring it all back after the relaunch.
//
// Everything runs through the CLI: `work snapshot --json` writes the file,
// `work snapshots --json` lists them, `work restore [--dry-run] --json`
// remakes the tabs that are gone and resumes each Claude conversation
// (`claude --resume <session id>`, Claude Code's own state being its
// transcript). The screen shows what is open now, what the latest snapshot
// holds, and three keys: s save, d dry run, r restore. A restore takes
// about two seconds per tab (it2), so it runs in the background and its
// result lines land on the screen when it is done.
//
// Stated limits: a turn that was running when the machine went down is
// lost (the last finished turn is what comes back); restore never closes
// anything, so tabs iTerm2 restored on its own stay next to the new ones;
// plain tabs come back as single panes; the screen has no note field
// (`savannah work snapshot --note "..."` does).

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// snapshotRow mirrors one row of `work snapshots --json`.
type snapshotRow struct {
	Stamp      string `json:"stamp"`
	Path       string `json:"path"`
	Taken      int64  `json:"taken"`
	TakenLocal string `json:"taken_local"`
	Work       int    `json:"work"`
	Live       int    `json:"live"`
	Plain      int    `json:"plain"`
	Note       string `json:"note"`
}

// restoreResult mirrors `work restore --json` (and its --dry-run plan).
type restoreResult struct {
	Stamp    string           `json:"stamp"`
	DryRun   bool             `json:"dry_run"`
	Restored []restoredTab    `json:"restored"`
	Plain    []restoredPlain  `json:"plain"`
	Skipped  []restoreSkipped `json:"skipped"`
	Failed   []restoreFailed  `json:"failed"`
}

// One item of a restore: a work session's tab remade, a plain tab remade,
// one left alone (why) or one that could not be remade (error).
type restoredTab struct {
	Name  string `json:"name"`
	How   string `json:"how"`
	TabID string `json:"tab_id"`
}
type restoredPlain struct {
	Project string `json:"project"`
	How     string `json:"how"`
	TabID   string `json:"tab_id"`
}
type restoreSkipped struct {
	Name    string `json:"name"`
	Project string `json:"project"`
	Why     string `json:"why"`
}
type restoreFailed struct {
	Name    string `json:"name"`
	Project string `json:"project"`
	Error   string `json:"error"`
}

func (s restoreSkipped) who(home string) string { return whoOf(s.Name, s.Project, home) }
func (f restoreFailed) who(home string) string  { return whoOf(f.Name, f.Project, home) }

// tabSuffix is ", tab 14" for a result line, "" when the tab has no id (a
// dry run makes none).
func tabSuffix(id string) string {
	if id == "" {
		return ""
	}
	return ", tab " + id
}

type snapshotsMsg struct {
	rows []snapshotRow
	err  string
}
type snapshotDoneMsg struct {
	path  string
	work  int
	live  int
	plain int
	err   string
}
type restoreDoneMsg struct {
	dry bool
	res restoreResult
	err string
}

type snapState struct {
	rows   []snapshotRow // newest first
	err    string
	busy   string   // "saving...", "restoring..." while a command runs
	lines  []string // the last command's result, line by line
	known  bool     // the list has been fetched at least once
	hinted bool     // the startup hint has been decided (shown or not), once
}

func snapshotsArgs() []string { return workArgs("snapshots", "", "--json") }
func snapshotArgs() []string  { return workArgs("snapshot", "", "--json") }
func restoreArgs(dry bool) []string {
	if dry {
		return workArgs("restore", "", "--dry-run", "--json")
	}
	return workArgs("restore", "", "--json")
}

func (m model) snapshotsCmd() tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		rows, errText := runJSON[[]snapshotRow](run, bin, snapshotsArgs(), "work snapshots printed no list")
		return snapshotsMsg{rows: rows, err: errText}
	}
}

func (m model) snapshotCmd() tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		r, errText := runJSON[struct {
			Path  string `json:"path"`
			Work  int    `json:"work"`
			Live  int    `json:"live"`
			Plain int    `json:"plain"`
		}](run, bin, snapshotArgs(), "work snapshot printed no result")
		return snapshotDoneMsg{path: r.Path, work: r.Work, live: r.Live, plain: r.Plain, err: errText}
	}
}

func (m model) restoreCmd(dry bool) tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		out, err := run(bin, restoreArgs(dry)...)
		var res restoreResult
		// A restore with one failed item exits 1 but still prints its JSON.
		if jerr := json.Unmarshal(out, &res); jerr != nil {
			if err != nil {
				return restoreDoneMsg{dry: dry, err: cliError(err)}
			}
			return restoreDoneMsg{dry: dry, err: "work restore printed no result"}
		}
		return restoreDoneMsg{dry: dry, res: res}
	}
}

func (m *model) enterSnapshot() tea.Cmd {
	m.mode = modeSnapshot
	m.snap.lines, m.snap.busy, m.snap.err = nil, "", ""
	m.status = ""
	slog.Debug("snapshot screen opened")
	return m.snapshotsCmd()
}

func (m *model) leaveSnapshot() { m.mode = modeBoard }

// snapshotHint runs once, when both the sessions and the snapshot list have
// been read: after a relaunch, named sessions offline and a snapshot from
// the last two days mean the status line should say Ctrl-S brings them back.
func (m *model) snapshotHint() {
	if m.snap.hinted || !m.snap.known || m.sessTicks == 0 {
		return
	}
	m.snap.hinted = true
	if m.mode != modeBoard || m.status != "" || len(m.snap.rows) == 0 {
		return
	}
	named, live, _ := m.openNow()
	latest := m.snap.rows[0]
	if named > live && time.Since(time.Unix(latest.Taken, 0)) < 48*time.Hour {
		slog.Info("snapshot hint", "stamp", latest.Stamp, "offline", named-live)
		m.status = helpStyle.Render(fmt.Sprintf("snapshot %s holds %d session%s; Ctrl-S restores the ones that are gone",
			latest.Stamp, latest.Work, plural(latest.Work)))
	}
}

// openNow counts the board's sessions for the screen: named sessions, live
// and offline (whether HISTORY shows them or not), and plain tabs.
func (m model) openNow() (named, live, plain int) {
	for _, s := range m.sessions {
		switch {
		case s.Work == "":
			plain++
		default:
			named++
			if !s.closed() {
				live++
			}
		}
	}
	return
}

// restoreLines renders a restore result (or plan) one line per item.
func restoreLines(res restoreResult, home string) []string {
	var out []string
	verb := "restored"
	if res.DryRun {
		verb = "would restore"
	}
	for _, r := range res.Restored {
		out = append(out, fmt.Sprintf("%s %s (%s%s)", verb, r.Name, r.How, tabSuffix(r.TabID)))
	}
	for _, p := range res.Plain {
		out = append(out, fmt.Sprintf("%s plain tab %s (%s%s)", verb, tilde(p.Project, home), p.How, tabSuffix(p.TabID)))
	}
	for _, s := range res.Skipped {
		out = append(out, "skipped "+s.who(home)+": "+s.Why)
	}
	for _, f := range res.Failed {
		out = append(out, warnStyle.Render("failed "+f.who(home)+": "+f.Error))
	}
	if len(out) == 0 {
		out = append(out, "nothing to do: every session in the snapshot is open")
	}
	return out
}

func (m model) updateSnapshot(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case snapshotsMsg:
		m.snap.known = true
		if msg.err != "" {
			m.snap.err = msg.err
		} else {
			m.snap.rows, m.snap.err = msg.rows, ""
		}
		m.snapshotHint()
		return m, nil
	case snapshotDoneMsg:
		m.snap.busy = ""
		if msg.err != "" {
			m.snap.lines = []string{warnStyle.Render("snapshot: " + clip(msg.err))}
			return m, nil
		}
		slog.Info("snapshot saved", "path", msg.path, "work", msg.work, "live", msg.live, "plain", msg.plain)
		m.snap.lines = []string{okStyle.Render(fmt.Sprintf("saved %s: %d work sessions (%d live), %d plain tabs",
			tilde(msg.path, m.home), msg.work, msg.live, msg.plain))}
		return m, m.snapshotsCmd()
	case restoreDoneMsg:
		m.snap.busy = ""
		if msg.err != "" {
			m.snap.lines = []string{warnStyle.Render("restore: " + clip(msg.err))}
			return m, nil
		}
		slog.Info("restore done", "dry", msg.dry, "restored", len(msg.res.Restored), "plain", len(msg.res.Plain),
			"skipped", len(msg.res.Skipped), "failed", len(msg.res.Failed))
		m.snap.lines = restoreLines(msg.res, m.home)
		if msg.dry {
			return m, nil
		}
		return m, readSessionsCmd()
	}
	return m, nil
}

func (m model) updateSnapshotKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab:
		m.leaveSnapshot()
		return m, nil
	case tea.KeyRunes:
		if m.snap.busy != "" {
			return m, nil
		}
		switch k.String() {
		case "s":
			action("snapshot")
			m.snap.busy, m.snap.lines = "saving...", nil
			return m, m.snapshotCmd()
		case "d":
			action("restore", "dry", true)
			m.snap.busy, m.snap.lines = "planning...", nil
			return m, m.restoreCmd(true)
		case "r":
			if len(m.snap.rows) == 0 {
				m.snap.lines = []string{warnStyle.Render("no snapshot yet: s saves one")}
				return m, nil
			}
			action("restore", "stamp", m.snap.rows[0].Stamp)
			m.snap.busy, m.snap.lines = "restoring (about two seconds per tab)...", nil
			return m, m.restoreCmd(false)
		}
	}
	return m, nil
}

func (m model) viewSnapshot() string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s   %s\n\n", headerStyle.Render("savannah  snapshot"), clock())
	named, live, plain := m.openNow()
	line := func(s string) { b.WriteString(fit(s, m.width) + "\n") } // one row each, whatever the width
	line(fmt.Sprintf("  Open now: %d work session%s (%d live, %d offline), %d plain tab%s",
		named, plural(named), live, named-live, plain, plural(plain)))
	switch {
	case !m.snap.known:
		b.WriteString("  Snapshots: fetching...\n")
	case m.snap.err != "":
		b.WriteString("  " + warnStyle.Render("snapshots: "+clip(m.snap.err)) + "\n")
	case len(m.snap.rows) == 0:
		b.WriteString("  No snapshot yet.\n")
	default:
		r := m.snap.rows[0]
		note := ""
		if r.Note != "" {
			note = ", note " + r.Note
		}
		line(fmt.Sprintf("  Latest snapshot: %s (%s%s): %d work session%s (%d live), %d plain tab%s",
			r.Stamp, r.TakenLocal, note, r.Work, plural(r.Work), r.Live, r.Plain, plural(r.Plain)))
		if n := len(m.snap.rows) - 1; n > 0 {
			older := []string{}
			for _, o := range m.snap.rows[1:] {
				if len(older) == 4 {
					break
				}
				older = append(older, o.Stamp)
			}
			more := ""
			if n > 4 {
				more = "  ..."
			}
			line("  Older: " + strings.Join(older, "  ") + more)
		}
	}
	b.WriteString("\n")
	b.WriteString("  " + headerStyle.Render("s") + "  save a snapshot now: every work session with its tab, worktree and Claude\n")
	b.WriteString("     conversation id, plus every plain tab; then reboot. `savannah work restore` or this\n")
	b.WriteString("     screen brings it back.\n")
	b.WriteString("  " + headerStyle.Render("r") + "  restore the latest: remake each tab that is gone, in its worktree, with\n")
	b.WriteString("     `claude --resume` on its conversation; what is still open is left alone.\n")
	b.WriteString("  " + headerStyle.Render("d") + "  dry run: show what r would do, touch nothing.\n\n")
	if m.snap.busy != "" {
		b.WriteString("  " + m.snap.busy + "\n")
	}
	for _, ln := range m.snap.lines {
		b.WriteString("  " + fit(ln, m.width-2) + "\n")
	}
	b.WriteString("\n" + helpStyle.Render("s save   d dry run   r restore   Esc back   Ctrl-C quit"))
	return b.String()
}
