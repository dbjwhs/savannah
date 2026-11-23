// recap.go: where each session of a section stands (Ctrl-Q), on one screen.
//
// Back after days away, NOW is a column of names. Ctrl-Q runs `savannah
// work recap --section <the highlighted row's section> --json` (work/):
// for every work session there, what is already on disk: its title and
// branch, when it was last active, the summary its own Claude last gave,
// the last note, the owner's last prompt and the end of the last reply.
// Nothing is typed into any tab and no Claude is called, so it is free,
// instant, and works for a tab that is closed.
//
// On the screen, 1 2 3 switch to NOW, ACTIVE or both, r reads again, and
// `a` asks: the same recap with --ask, which first types the `work ask`
// question into the sessions of those sections that are safe to interrupt
// (idle or done), waits for the replies (up to two minutes), and shows the
// recap with the fresh summaries. That spends one turn of each asked
// session, which is why it is a key of its own and not what Ctrl-Q does.

package main

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

// recapOut mirrors `work recap --json`.
type recapOut struct {
	Sections []string     `json:"sections"`
	Sessions []recapEntry `json:"sessions"`
	Ask      *askOut      `json:"ask"`
}

type recapEntry struct {
	Name       string `json:"name"`
	Title      string `json:"title"`
	Section    string `json:"section"`
	Branch     string `json:"branch"`
	State      string `json:"state"`
	LastActive int64  `json:"last_active"`
	Summary    string `json:"summary"`
	SummaryAt  int64  `json:"summary_at"`
	LastNote   string `json:"last_note"`
	LastPrompt string `json:"last_prompt"`
	Reply      string `json:"reply"`
	ReplyCut   int    `json:"reply_cut"`
	Turns      int    `json:"turns_total"`
}

type recapMsg struct {
	out recapOut
	err string
}

type recapState struct {
	busy     bool   // a `work recap` is running
	asking   bool   // and it is one with --ask
	sections string // what is shown or being read: "now", "active", "now,active", ...
	text     string // the last result, rendered
}

func recapArgs(sections string, ask bool) []string {
	args := []string{"work", "recap", "--section", sections, "--json"}
	if ask {
		args = append(args, "--ask")
	}
	return args
}

func recapCmd(bin, sections string, ask bool) tea.Cmd {
	return func() tea.Msg {
		res, errText := runJSON[recapOut](run, bin, recapArgs(sections, ask), "work recap printed no JSON")
		return recapMsg{out: res, err: errText}
	}
}

// recapSection is the section Ctrl-Q recaps: the highlighted row's, NOW
// when no session row is highlighted.
func (m model) recapSection() string {
	r, ok := m.currentRow()
	if !ok || r.kind != rowSession {
		return strings.ToLower(sectionName(secNow))
	}
	return strings.ToLower(sectionName(sectionOf(r.s, recordMap(m.work))))
}

// pressRecap is Ctrl-Q on the board.
func (m model) pressRecap() (model, tea.Cmd) {
	if m.recap.busy {
		m.status = helpStyle.Render("still reading the recap...")
		return m, nil
	}
	return m.startRecap(m.recapSection(), false)
}

// startRecap runs the CLI; the screen opens (or is redrawn) when it returns.
func (m model) startRecap(sections string, ask bool) (model, tea.Cmd) {
	m.recap.busy = true
	m.recap.asking = ask
	m.recap.sections = sections
	what := "reading where " + strings.ToUpper(sections) + " stands..."
	if ask {
		what = "asking " + strings.ToUpper(sections) + " for fresh summaries (one turn each, up to two minutes; working and waiting tabs are skipped)..."
	}
	m.act("recap", what, "sections", sections, "ask", ask)
	if m.mode == modeRecap {
		m.refreshRecap()
	}
	return m, recapCmd(m.bin, sections, ask)
}

// recapLines renders a result: a header per section, a block per session.
func recapLines(o recapOut, now int64) []string {
	var out []string
	if len(o.Sessions) == 0 {
		out = append(out, "no work sessions in "+strings.ToUpper(strings.Join(o.Sections, ", ")), "")
	}
	section := ""
	for _, e := range o.Sessions {
		if e.Section != section {
			section = e.Section
			n := 0
			for _, x := range o.Sessions {
				if x.Section == section {
					n++
				}
			}
			out = append(out, headerStyle.Render(fmt.Sprintf("%s (%d)", strings.ToUpper(section), n)), "")
		}
		head := headerStyle.Render(e.Name)
		rest := []string{}
		if e.Title != "" {
			rest = append(rest, e.Title)
		}
		if e.Branch != "" {
			rest = append(rest, e.Branch)
		}
		rest = append(rest, e.State)
		if e.LastActive > 0 {
			rest = append(rest, "last active "+humanAge(now-e.LastActive)+" ago")
		}
		out = append(out, head+helpStyle.Render("   "+strings.Join(rest, "   ")))
		if e.Summary != "" {
			label := "summary"
			if e.SummaryAt > 0 {
				label += " (" + humanAge(now-e.SummaryAt) + " ago)"
			}
			out = append(out, helpStyle.Render(label+":"))
			for _, line := range strings.Split(strings.TrimSpace(e.Summary), "\n") {
				out = append(out, "  "+line)
			}
		}
		if e.LastNote != "" {
			out = append(out, helpStyle.Render("note: ")+e.LastNote)
		}
		if e.LastPrompt != "" {
			out = append(out, helpStyle.Render("you: ")+e.LastPrompt)
		}
		if e.Reply != "" {
			label := "claude:"
			if e.ReplyCut > 0 {
				label = fmt.Sprintf("claude (the end; %d lines above, Ctrl-O on the board shows all):", e.ReplyCut)
			}
			out = append(out, helpStyle.Render(label))
			for _, line := range strings.Split(e.Reply, "\n") {
				out = append(out, "  "+line)
			}
		} else if e.Turns == 0 {
			out = append(out, helpStyle.Render("(no transcript found: nothing was asked there yet, or it is gone)"))
		}
		out = append(out, "")
	}
	if o.Ask != nil {
		answered := 0
		for _, a := range o.Ask.Asked {
			if a.How == "answered" {
				answered++
			}
		}
		out = append(out, helpStyle.Render(fmt.Sprintf("asked %d, %d answered in %.0f s", len(o.Ask.Asked), answered, o.Ask.Waited)))
		for _, a := range o.Ask.Asked {
			if a.How != "answered" {
				out = append(out, warnStyle.Render("no reply yet from "+a.Name+" ("+a.How+")"))
			}
		}
		for _, s := range o.Ask.Skipped {
			out = append(out, helpStyle.Render("skipped "+s.Name+": "+s.Why))
		}
		for _, f := range o.Ask.Failed {
			out = append(out, warnStyle.Render("failed "+f.Name+": "+f.Error))
		}
	}
	return out
}

// updateRecap takes the CLI's result and shows it.
func (m model) updateRecap(msg recapMsg) (tea.Model, tea.Cmd) {
	asked := m.recap.asking
	m.recap.busy = false
	m.recap.asking = false
	if msg.err != "" {
		m.fail("recap failed", "recap", msg.err)
		if m.mode == modeRecap {
			m.refreshRecap()
		}
		return m, nil
	}
	slog.Info("recap done", "sections", m.recap.sections, "sessions", len(msg.out.Sessions), "asked", asked)
	m.recap.text = strings.Join(recapLines(msg.out, time.Now().Unix()), "\n")
	m.status = ""
	switch m.mode {
	case modeBoard:
		m.enterRecap()
	case modeRecap:
		m.refreshRecap()
		m.vp.GotoTop()
	}
	if asked {
		return m, readSessionsCmd() // the summaries on the board are new too
	}
	return m, nil
}

func (m *model) enterRecap() {
	m.mode = modeRecap
	m.vp = viewport.New(m.width, m.bodyHeight(recapChrome))
	m.refreshRecap()
}

const recapChrome = 4 // header, blank, blank, help

func (m *model) refreshRecap() {
	m.vp.Width = m.width
	m.vp.Height = m.bodyHeight(recapChrome)
	m.vp.SetContent(wrapTo(m.recap.text, m.width))
}

func (m model) updateRecapKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab, tea.KeyCtrlQ:
		m.mode = modeBoard
		return m, nil
	case tea.KeyRunes:
		if m.recap.busy {
			return m, nil
		}
		switch k.String() {
		case "a":
			return m.startRecap(m.recap.sections, true)
		case "r":
			return m.startRecap(m.recap.sections, false)
		case "1":
			return m.startRecap("now", false)
		case "2":
			return m.startRecap("active", false)
		case "3":
			return m.startRecap("now,active", false)
		}
	}
	scrollKey(&m.vp, nil, k.Type)
	return m, nil
}

func (m model) viewRecap() string {
	var b strings.Builder
	head := "savannah  recap of " + strings.ToUpper(m.recap.sections)
	fmt.Fprintf(&b, "%s   %s", headerStyle.Render(head), clock())
	if m.status != "" {
		b.WriteString("   " + m.status) // reading, asking, or why the last read failed
	}
	b.WriteString("\n\n")
	b.WriteString(m.vp.View())
	b.WriteString("\n" + helpStyle.Render("1 NOW   2 ACTIVE   3 both   r read again   a ask them for fresh summaries (one turn each)   Up/Down PgUp/PgDn scroll   Esc back"))
	return b.String()
}
