// ask.go: one question to every tab (Ctrl-A twice), the replies on one
// screen.
//
// "Ask all my Claudes what they are doing" runs through `savannah work ask
// --all --json` (work/): the question is typed into every live tab that is
// safe to interrupt (idle or done; a working tab is not interrupted, a
// waiting tab has a permission prompt or a question up that typed text
// would answer), the CLI waits for each tab's hook file to say it has
// stopped and reads the reply from its transcript, and each work session's
// reply is kept as its summary (the detail panel shows it). While the CLI
// waits the board stays up: the asked rows turn yellow (working) and back
// to green (done) in front of you; when the CLI returns the replies open
// as a screen (Esc back). Each asked tab spends one turn of its own
// context, so the key asks twice: the first press says how many tabs it
// would ask, the second asks.
//
// Stated limits: a tab whose Claude is at a question of its own that the
// hook does not know about (no Notification for it) is asked anyway; a
// compacted session answers from what it still remembers; a tab that
// takes longer than the CLI's timeout (120 s) shows as a timeout here and
// its reply is one Ctrl-O away later.

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

// askOut mirrors `work ask --json`.
type askOut struct {
	Question string  `json:"question"`
	Timeout  float64 `json:"timeout"`
	Waited   float64 `json:"waited"`
	Asked    []struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		How     string `json:"how"` // answered | timeout | sent
		Reply   string `json:"reply"`
	} `json:"asked"`
	Skipped []struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		Why     string `json:"why"`
	} `json:"skipped"`
	Failed []struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		Error   string `json:"error"`
	} `json:"failed"`
}

type askMsg struct {
	out askOut
	err string
}

type askState struct {
	armed   bool // the first Ctrl-A was pressed; the next one asks
	busy    bool // a `work ask` is running
	started time.Time
	text    string // the last result, rendered (the screen's content)
	have    bool   // a result has arrived at least once
}

func askArgs() []string { return []string{"work", "ask", "--all", "--json"} }

func askCmd(bin string) tea.Cmd {
	return func() tea.Msg {
		out, err := run(bin, askArgs()...)
		if err != nil {
			return askMsg{err: cliError(err)}
		}
		var res askOut
		if json.Unmarshal(out, &res) != nil {
			return askMsg{err: "work ask printed no JSON"}
		}
		return askMsg{out: res}
	}
}

// askable counts the live tabs the CLI would ask now: idle or done, with a
// pane. The CLI decides for real (it also skips the asking session).
func (m model) askable() int {
	n := 0
	for _, s := range m.sessions {
		if (s.State == "idle" || s.State == "done") && s.ITerm != "" {
			n++
		}
	}
	return n
}

// pressAsk is Ctrl-A on the board: the last replies when there are some,
// else arm, then ask (the screen's `a` asks again).
func (m model) pressAsk() (tea.Model, tea.Cmd) {
	if m.ask.busy {
		m.status = helpStyle.Render("still asking (the rows show who has answered)...")
		return m, nil
	}
	if m.ask.have && !m.ask.armed {
		m.enterAsk()
		return m, nil
	}
	if !m.ask.armed {
		n := m.askable()
		if n == 0 {
			m.status = warnStyle.Render("no tab to ask: none is idle or done")
			return m, nil
		}
		m.ask.armed = true
		m.status = warnStyle.Render(fmt.Sprintf("Ctrl-A again to ask %d tab%s what they are doing (one turn each; working and waiting tabs are skipped)",
			n, plural(n)))
		return m, nil
	}
	return m.startAsk()
}

// startAsk runs the CLI and goes back to the board to watch the rows.
func (m model) startAsk() (tea.Model, tea.Cmd) {
	m.ask.armed = false
	m.ask.busy = true
	m.ask.started = time.Now()
	m.mode = modeBoard
	m.status = "asking every idle tab what it is doing (up to two minutes; the rows show who has answered)..."
	slog.Info("action", "do", "ask", "tabs", m.askable())
	return m, askCmd(m.bin)
}

// askLines renders a result, one block per asked tab.
func askLines(o askOut, home string) []string {
	var out []string
	out = append(out, helpStyle.Render("asked: "+o.Question), "")
	for _, a := range o.Asked {
		who := a.Name
		if who == "" {
			who = tilde(a.Project, home)
		}
		head := headerStyle.Render(who) + helpStyle.Render("   "+tilde(a.Project, home))
		switch a.How {
		case "answered":
			out = append(out, head)
			if a.Reply == "" {
				out = append(out, "(stopped without a reply)")
			} else {
				out = append(out, strings.Split(strings.TrimSpace(a.Reply), "\n")...)
			}
		case "timeout":
			out = append(out, head+warnStyle.Render("   no reply within the timeout (Ctrl-O shows it when it comes)"))
		default:
			out = append(out, head+helpStyle.Render("   "+a.How))
		}
		out = append(out, "")
	}
	for _, s := range o.Skipped {
		who := s.Name
		if who == "" {
			who = tilde(s.Project, home)
		}
		out = append(out, helpStyle.Render("skipped "+who+": "+s.Why))
	}
	for _, f := range o.Failed {
		who := f.Name
		if who == "" {
			who = tilde(f.Project, home)
		}
		out = append(out, warnStyle.Render("failed "+who+": "+f.Error))
	}
	if len(o.Asked) == 0 {
		out = append(out, "no tab asked")
	}
	return out
}

// askStatus is the board's status line for a finished run.
func askStatus(o askOut) string {
	answered, late := 0, 0
	for _, a := range o.Asked {
		if a.How == "answered" {
			answered++
		} else {
			late++
		}
	}
	line := fmt.Sprintf("%d tab%s answered in %.0f s", answered, plural(answered), o.Waited)
	if late > 0 {
		line += fmt.Sprintf(", %d did not in time", late)
	}
	if len(o.Skipped) > 0 {
		line += fmt.Sprintf(", %d skipped", len(o.Skipped))
	}
	return okStyle.Render(line + "; Ctrl-A shows the replies")
}

// updateAsk takes the CLI's result: the screen opens when the board is in
// front, else the status line says the replies are waiting.
func (m model) updateAsk(msg askMsg) (tea.Model, tea.Cmd) {
	m.ask.busy = false
	if msg.err != "" {
		slog.Warn("ask failed", "error", msg.err)
		m.status = warnStyle.Render("ask: " + clip(msg.err))
		return m, readSessionsCmd()
	}
	o := msg.out
	slog.Info("ask done", "asked", len(o.Asked), "skipped", len(o.Skipped), "failed", len(o.Failed), "waited", o.Waited)
	m.ask.text = strings.Join(askLines(o, m.home), "\n")
	m.ask.have = true
	m.status = askStatus(o)
	if m.mode == modeBoard {
		m.enterAsk()
	}
	return m, readSessionsCmd()
}

func (m *model) enterAsk() {
	m.mode = modeAsk
	m.vp = viewport.New(m.width, m.askHeight())
	m.refreshAsk()
}

func (m *model) leaveAsk() { m.mode = modeBoard }

const askChrome = 4 // header, blank, blank, help

func (m model) askHeight() int {
	h := m.height - askChrome
	if h < 3 {
		h = 3
	}
	return h
}

func (m *model) refreshAsk() {
	m.vp.Width = m.width
	m.vp.Height = m.askHeight()
	m.vp.SetContent(wrapTo(m.ask.text, m.width))
}

func (m model) updateAskKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab, tea.KeyCtrlA:
		m.leaveAsk()
		return m, nil
	case tea.KeyUp:
		m.vp.ScrollUp(1)
	case tea.KeyDown:
		m.vp.ScrollDown(1)
	case tea.KeyPgUp:
		m.vp.PageUp()
	case tea.KeyPgDown:
		m.vp.PageDown()
	case tea.KeyHome:
		m.vp.GotoTop()
	case tea.KeyEnd:
		m.vp.GotoBottom()
	case tea.KeyRunes:
		if k.String() == "a" && !m.ask.busy {
			return m.startAsk()
		}
	}
	return m, nil
}

func (m model) viewAsk() string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s   %s\n\n", headerStyle.Render("savannah  replies"), time.Now().Format("15:04:05"))
	b.WriteString(m.vp.View())
	b.WriteString("\n" + helpStyle.Render("a ask again (one turn per tab)   Up/Down PgUp/PgDn scroll   Esc back   Ctrl-C quit"))
	return b.String()
}
