// autoname.go: names for the tabs you opened yourself, when the board comes
// up (and for each new plain tab once something has been typed into it).
//
// A plain tab (a hook session no record claims) shows only its iTerm2 tab
// title, which is whatever the shell or Claude Code set. The owner asked
// for a good name per tab, found by asking the sessions what they are
// doing. The board does that through `savannah work autoname --json
// <ids>` (work/): a headless Claude titles an excerpt of each tab's own
// transcript, a name is derived, the tab is adopted. Nothing is typed into
// a live tab (a question typed there would be a prompt, or the answer to a
// permission prompt). The board submits each plain tab once per run, only
// once it has a prompt (the hook's last_prompt), one `autoname` at a time
// for all of them; the result lands on the status line, and the names
// appear on the next sessions read. Ctrl-R edits a title the owner does
// not like; `savannah work rename` changes the name.
//
// Off with --no-autoname for one run, or "autoname": "off" in the board
// file ($SAVANNAH_BOARD_FILE) for good. Stated limit: a tab whose first
// prompt arrives after the board has tried it is not retried until the
// next board start; a tab with no hook file yet (a trust prompt) is not a
// row at all until it has one.

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

// namedTab is one entry of `work autoname --json`'s "named" list.
type namedTab struct {
	Session     string `json:"session_id"`
	Project     string `json:"project"`
	Name        string `json:"name"`
	Title       string `json:"title"`
	How         string `json:"how"` // "claude" | "prompt"
	ClaudeError string `json:"claude_error"`
}

// autonameOut is `work autoname --json`'s output.
type autonameOut struct {
	Named   []namedTab `json:"named"`
	Skipped []struct {
		Session string `json:"session_id"`
		Why     string `json:"why"`
	} `json:"skipped"`
	Failed []struct {
		Session string `json:"session_id"`
		Error   string `json:"error"`
	} `json:"failed"`
}

type autonameMsg struct {
	ids []string
	out autonameOut
	err string
}

// pendingAutoname is the plain live tabs to name now: a hook session no
// record claims, with a prompt typed into it, not yet submitted this run.
// Nothing while naming is off or a run is in flight.
func (m model) pendingAutoname() []string {
	if !m.autoname || m.naming {
		return nil
	}
	var ids []string
	for _, s := range m.sessions {
		if s.Work != "" || isWorkRow(s) || s.LastPrompt == "" || m.nameTried[s.ID] {
			continue
		}
		ids = append(ids, s.ID)
	}
	sort.Strings(ids) // the attention sort is not an order worth keeping here
	return ids
}

// startAutoname submits the pending tabs in one `work autoname`, marks them
// tried, and says so on the status line. nil when there is nothing to do.
func (m *model) startAutoname() tea.Cmd {
	ids := m.pendingAutoname()
	if len(ids) == 0 {
		return nil
	}
	if m.nameTried == nil {
		m.nameTried = map[string]bool{}
	}
	for _, id := range ids {
		m.nameTried[id] = true
	}
	m.naming = true
	m.status = fmt.Sprintf("naming %d tab%s from what they are doing...", len(ids), plural(len(ids)))
	slog.Info("action", "do", "autoname", "tabs", len(ids))
	return autonameCmd(m.bin, ids)
}

// autonameArgs is the argv of one `work autoname --json <ids...>`.
func autonameArgs(ids []string) []string {
	args := append([]string{"work", "autoname", "--json"}, ids...)
	return args
}

func autonameCmd(bin string, ids []string) tea.Cmd {
	args := autonameArgs(ids)
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return autonameMsg{ids: ids, err: cliError(err)}
		}
		var res autonameOut
		if json.Unmarshal(out, &res) != nil {
			return autonameMsg{ids: ids, err: "work autoname printed no JSON"}
		}
		return autonameMsg{ids: ids, out: res}
	}
}

// autonameStatus is the status line for a finished run: the titles and
// names given, how many were skipped or failed, or the CLI's error.
func autonameStatus(msg autonameMsg) string {
	if msg.err != "" {
		return warnStyle.Render("naming tabs: " + clip(msg.err))
	}
	o := msg.out
	if len(o.Named) == 0 {
		line := "no tab named"
		if len(o.Skipped) > 0 {
			line += fmt.Sprintf(" (%d skipped: %s)", len(o.Skipped), o.Skipped[0].Why)
		}
		if len(o.Failed) > 0 {
			line += fmt.Sprintf(" (%d failed: %s)", len(o.Failed), o.Failed[0].Error)
		}
		return helpStyle.Render(clip(line))
	}
	var parts []string
	fallback := 0
	for _, n := range o.Named {
		label := n.Title
		if label == "" {
			label = n.Name
		} else if n.Name != "" && n.Name != n.Title {
			label += " (" + n.Name + ")"
		}
		parts = append(parts, label)
		if n.How != "claude" {
			fallback++
		}
	}
	line := fmt.Sprintf("named %d tab%s: %s", len(o.Named), plural(len(o.Named)), strings.Join(parts, ", "))
	if fallback > 0 {
		line += fmt.Sprintf("; %d from the first prompt (claude unavailable)", fallback)
	}
	if len(o.Failed) > 0 {
		line += fmt.Sprintf("; %d failed: %s", len(o.Failed), o.Failed[0].Error)
		return warnStyle.Render(clip(line))
	}
	return okStyle.Render(clip(line))
}
