// recover.go: the recover half of the Ctrl-S screen, and the hint on start.
//
// After a reboot, a crash or a forced update every Claude is gone, while
// the hook's status files still say which conversations were open and
// where. `work recover --json` starts each one again (`claude --resume
// <session id>`): in its own pane when iTerm2 brought the tab back, else an
// idle pane in its directory, else a new tab. No snapshot is needed. The
// board asks for the plan (`--dry-run`) once on start, and says so on the
// status line when there is something to recover; on the Ctrl-S screen c
// shows the plan and v does it.

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"

	tea "github.com/charmbracelet/bubbletea"
)

// recoverResult mirrors `work recover --json` (and its --dry-run plan).
type recoverResult struct {
	DryRun    bool             `json:"dry_run"`
	Recovered []recoveredItem  `json:"recovered"`
	Skipped   []recoverSkipped `json:"skipped"`
	Failed    []recoverFailed  `json:"failed"`
}

// One session of a recover: its Claude started again (where: "pane" = a
// pane that was open, "tab" = a new tab), left alone (why), or failed.
type recoveredItem struct {
	Name    string `json:"name"`
	Project string `json:"project"`
	How     string `json:"how"`
	Where   string `json:"where"`
}
type recoverSkipped struct {
	Name    string `json:"name"`
	Project string `json:"project"`
	Why     string `json:"why"`
}
type recoverFailed struct {
	Name    string `json:"name"`
	Project string `json:"project"`
	Error   string `json:"error"`
}

// recoverDoneMsg is a recover's result; hint marks the plan the board asks
// for on start, which only ever becomes a status line.
type recoverDoneMsg struct {
	dry  bool
	hint bool
	res  recoverResult
	err  string
}

func recoverArgs(dry bool) []string {
	if dry {
		return workArgs("recover", "", "--dry-run", "--json")
	}
	return workArgs("recover", "", "--json")
}

func (m model) recoverCmd(dry, hint bool) tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		out, err := run(bin, recoverArgs(dry)...)
		var res recoverResult
		// A recover with one failed session exits 1 but still prints its JSON.
		if jerr := json.Unmarshal(out, &res); jerr != nil {
			if err != nil {
				return recoverDoneMsg{dry: dry, hint: hint, err: cliError(err)}
			}
			return recoverDoneMsg{dry: dry, hint: hint, err: "work recover printed no result"}
		}
		return recoverDoneMsg{dry: dry, hint: hint, res: res}
	}
}

// recoverLines renders a recover result (or plan) one line per session. A
// session whose Claude runs is the normal case and is only counted.
func recoverLines(res recoverResult, home string) []string {
	var out []string
	verb := "recovered"
	if res.DryRun {
		verb = "would recover"
	}
	for _, r := range res.Recovered {
		where := "in a new tab"
		if r.Where == "pane" {
			where = "in its pane"
		}
		out = append(out, fmt.Sprintf("%s %s %s (%s)", verb, whoOf(r.Name, r.Project, home), where, r.How))
	}
	running := 0
	for _, s := range res.Skipped {
		if s.Why == "running" {
			running++
			continue
		}
		out = append(out, "skipped "+whoOf(s.Name, s.Project, home)+": "+s.Why)
	}
	for _, f := range res.Failed {
		out = append(out, warnStyle.Render("failed "+whoOf(f.Name, f.Project, home)+": "+f.Error))
	}
	if len(res.Recovered) == 0 && len(res.Failed) == 0 {
		out = append(out, fmt.Sprintf("nothing to recover: %d session%s running", running, plural(running)))
	}
	return out
}

func (m model) updateRecover(msg recoverDoneMsg) (tea.Model, tea.Cmd) {
	if msg.hint {
		// The plan asked for on start: a line on the board, when it is free.
		n := len(msg.res.Recovered)
		if msg.err == "" && n > 0 && m.mode == modeBoard && m.input.Value() == "" {
			slog.Info("recover hint", "sessions", n)
			m.status = warnStyle.Render(fmt.Sprintf(
				"%d session%s lost %s Claude (a reboot or a crash): Ctrl-S then v starts %s again",
				n, plural(n), map[bool]string{true: "its", false: "their"}[n == 1],
				map[bool]string{true: "it", false: "them"}[n == 1]))
		}
		return m, nil
	}
	m.snap.busy = ""
	if msg.err != "" {
		m.snap.lines = []string{warnStyle.Render("recover: " + clip(msg.err))}
		return m, nil
	}
	slog.Info("recover done", "dry", msg.dry, "recovered", len(msg.res.Recovered),
		"skipped", len(msg.res.Skipped), "failed", len(msg.res.Failed))
	m.snap.lines = recoverLines(msg.res, m.home)
	if msg.dry {
		return m, nil
	}
	return m, readSessionsCmd()
}
