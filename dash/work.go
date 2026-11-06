// work.go: the work-board half of the dash. Every key that acts on a work
// session (work/savannah_work.py: one iTerm2 tab per work item, Claude in a
// git worktree above a shell below) and the two screens behind them.
//
// The dash never touches a record, a worktree or a tab itself. Each action
// is one `savannah work <verb>` subprocess, the same CLI the owner types and
// the shim's work_* tools call, so the board can never disagree with the
// command line about what a session is:
//
//	Enter (empty)    work open <name> --json         focus the tab, or reopen it
//	Enter (text)     work say <name> -- <text>       type into its Claude
//	Ctrl-T, Enter    work note <name> -- <text>      append to notes.md
//	Ctrl-T on a      work adopt <name> --session=<id>  name the tab (it becomes a
//	  plain tab          [--title=<words>] --json     work session), then the note
//	Ctrl-R           work title <name> -- <words>    the title the board shows
//	                 work title <name> --clear       (the name stays; `work rename`
//	                                                 changes it); on a plain tab,
//	                 work adopt <name> --session=<id> [--title=<words>] --json
//	Shift-Up/Down    work move <name> --active|--later --rank=N  the section and
//	                                                 the order within it (order.go)
//	(rows, panel)    status.json, from `work status`  the tab's own progress
//	                                                 report (polling.go)
//	Ctrl-O           work output <name> --last 5     the last replies, refetched
//	(panel)          work output <name> --last 1     the highlighted row's last reply
//	Ctrl-X, Ctrl-X   work rm <name>                  close tab, remove worktree
//	Ctrl-N           work repos --json, then         the new-task dialog
//	                 work new <name> --repo=<path> [--prompt=<text>]
//	                          [--title=<words>] [--no-worktree] --json
//	Ctrl-S           work snapshots/snapshot/restore --json  save and restore
//	                                                 (snapshot.go)
//	Ctrl-L           (reads the log file itself)     the board's log (logview.go)
//
// A name is typed as a name (fix-parser) or as words (Fix the parser): the
// words become the title the WORK column shows and the name is derived from
// them (fix-the-parser); the name stays the identifier everywhere else.
//
// Values travel in the `=` form or after `--`, so text that begins with "-"
// is never read as an option (the CLI's own rule). A failed command comes
// back as a message carrying the CLI's stderr, which already says why (a
// dirty worktree, a pane that is gone), so the status line just shows it.
//
// Stated limits: the prompt line is one line (a multi-line prompt goes in
// through the tab itself); output is the CLI's human rendering of the last
// five turns, polled every two seconds, not a live stream; a repo typed as
// a unique prefix of a listed name is taken as that repo, so a relative
// path that happens to prefix a repo name must be written with "./".

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

const (
	outputTurns   = "5"             // `work output --last N` shown in the output view
	outputRefresh = 2 * time.Second // how often the output view refetches
	outputChrome  = 2               // header + help lines around the viewport
	statusClip    = 120             // runes of a CLI error kept on the status line
)

// Results of the work commands. err is "" on success, else the CLI's
// stderr (or the exec error) ready for the status line.
type workOpenMsg struct{ name, action, err string }
type workSaidMsg struct{ name, err string }
type workNotedMsg struct{ name, err string }
type workRmMsg struct{ name, err string }
type workNewMsg struct{ name, err string }
type workAdoptMsg struct{ name, title, err string }
type reposMsg struct {
	info reposInfo
	err  string
}

// workOutputMsg carries one fetch of the output view; seq orphans fetches
// and ticks from an earlier view the same way tailSeq does for the tail.
type workOutputMsg struct {
	seq  int
	name string
	text string // stdout, or the CLI's stderr when it failed
}
type outputTick struct{ seq int }

// reposInfo mirrors `work repos --json`: the owner's folder of repos (the
// hub directory, "" when none is remembered), the repo names in it, and the
// scratch directory for tasks that belong to no repo ("" when none).
type reposInfo struct {
	Dir     string   `json:"dir"`
	Picked  string   `json:"picked"` // "given", "remembered" or "cwd"
	Repos   []string `json:"repos"`
	Scratch string   `json:"scratch"`
}

// workArgs builds `work <sub> [<name>] ...` argv for the savannah CLI.
func workArgs(sub, name string, rest ...string) []string {
	args := []string{"work", sub}
	if name != "" {
		args = append(args, name)
	}
	return append(args, rest...)
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return cliError(err)
}

// clip keeps a CLI error to one status line: first line, capped.
func clip(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	r := []rune(s)
	if len(r) > statusClip {
		return string(r[:statusClip-1]) + "~"
	}
	return s
}

func (m model) workOpenCmd(name string) tea.Cmd {
	bin, args := m.bin, workArgs("open", name, "--json")
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return workOpenMsg{name: name, err: cliError(err)}
		}
		var r struct {
			Action string `json:"action"`
		}
		if json.Unmarshal(out, &r) != nil || r.Action == "" {
			r.Action = "opened"
		}
		return workOpenMsg{name: name, action: r.Action}
	}
}

func (m model) workSayCmd(name, text string) tea.Cmd {
	bin, args := m.bin, workArgs("say", name, "--", text)
	return func() tea.Msg {
		_, err := run(bin, args...)
		return workSaidMsg{name: name, err: errText(err)}
	}
}

func (m model) workNoteCmd(name, text string) tea.Cmd {
	bin, args := m.bin, workArgs("note", name, "--", text)
	return func() tea.Msg {
		_, err := run(bin, args...)
		return workNotedMsg{name: name, err: errText(err)}
	}
}

// workRmCmd runs the CLI's rm as is: a dirty worktree is refused there with
// the --force hint, and that refusal is exactly what the status line shows.
func (m model) workRmCmd(name string) tea.Cmd {
	bin, args := m.bin, workArgs("rm", name)
	return func() tea.Msg {
		_, err := run(bin, args...)
		return workRmMsg{name: name, err: errText(err)}
	}
}

func (m model) workNewCmd(name string, args []string) tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return workNewMsg{name: name, err: cliError(err)}
		}
		var rec struct {
			Name string `json:"name"`
		}
		if json.Unmarshal(out, &rec) != nil {
			return workNewMsg{name: name, err: "work new printed no record"}
		}
		return workNewMsg{name: name}
	}
}

// adoptArgs is the exact argv that names a plain tab: the hook session id
// travels in the `=` form so an odd id can never read as an option.
func adoptArgs(name, sessionID, title string) []string {
	args := workArgs("adopt", name, "--session="+sessionID)
	if title != "" {
		args = append(args, "--title="+title)
	}
	return append(args, "--json")
}

// workAdoptCmd turns a plain tab into the work session <name> (no new tab:
// the record points at the tab's directory and Claude pane), titled when
// the owner typed words rather than a name.
func (m model) workAdoptCmd(name, sessionID, title string) tea.Cmd {
	bin, args := m.bin, adoptArgs(name, sessionID, title)
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return workAdoptMsg{name: name, title: title, err: cliError(err)}
		}
		var rec struct {
			Name string `json:"name"`
		}
		if json.Unmarshal(out, &rec) != nil {
			return workAdoptMsg{name: name, title: title, err: "work adopt printed no record"}
		}
		return workAdoptMsg{name: name, title: title}
	}
}

func (m model) reposCmd() tea.Cmd {
	bin, args := m.bin, workArgs("repos", "", "--json")
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return reposMsg{err: cliError(err)}
		}
		var info reposInfo
		if json.Unmarshal(out, &info) != nil {
			return reposMsg{err: "work repos printed no list"}
		}
		return reposMsg{info: info}
	}
}

func (m model) workOutputCmd(seq int, name string) tea.Cmd {
	bin, args := m.bin, workArgs("output", name, "--last", outputTurns)
	return func() tea.Msg {
		out, err := poll(bin, args...)
		if err != nil {
			return workOutputMsg{seq: seq, name: name, text: cliError(err)}
		}
		return workOutputMsg{seq: seq, name: name, text: string(out)}
	}
}

func outputTickCmd(seq int) tea.Cmd {
	return tea.Tick(outputRefresh, func(time.Time) tea.Msg { return outputTick{seq: seq} })
}

// updateWork folds the work commands' results into the model. These arrive
// in any mode (a `new` can finish after Esc left the dialog), so each case
// checks where the user is now before touching a screen.
func (m model) updateWork(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case workOpenMsg:
		if msg.err != "" {
			slog.Warn("open failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("open " + msg.name + ": " + clip(msg.err))
			return m, nil
		}
		slog.Info("open done", "name", msg.name, "action", msg.action)
		m.status = okStyle.Render(msg.action + " " + msg.name)
		if msg.action == "reopened" {
			return m, readSessionsCmd()
		}
		return m, nil
	case workSaidMsg:
		if msg.err != "" {
			slog.Warn("say failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("say to " + msg.name + ": " + clip(msg.err))
		} else {
			slog.Info("said", "name", msg.name)
			m.status = okStyle.Render("said to " + msg.name)
		}
		return m, nil
	case workNotedMsg:
		if msg.err != "" {
			slog.Warn("note failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("note " + msg.name + ": " + clip(msg.err))
		} else {
			slog.Info("noted", "name", msg.name)
			m.status = okStyle.Render("noted " + msg.name)
		}
		return m, nil
	case workRmMsg:
		if msg.err != "" {
			slog.Warn("rm failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("rm " + msg.name + ": " + clip(msg.err))
			return m, nil
		}
		slog.Info("removed", "name", msg.name)
		m.status = okStyle.Render("removed " + msg.name)
		return m, readSessionsCmd()
	case workNewMsg:
		if msg.err != "" {
			slog.Warn("new failed", "name", msg.name, "err", msg.err)
			if m.mode == modeNew {
				// Keep what was typed so a typo is a one-field fix.
				m.nd.busy = false
				m.nd.err = clip(msg.err)
				return m, nil
			}
			m.status = warnStyle.Render("new " + msg.name + ": " + clip(msg.err))
			return m, nil
		}
		if m.mode == modeNew {
			m.mode = modeBoard
			m.nd = newDialog{}
		}
		slog.Info("new done", "name", msg.name)
		m.status = okStyle.Render("opened " + msg.name)
		return m, readSessionsCmd()
	case workAdoptMsg:
		if msg.err != "" {
			// The name stays in the box so a refused one is a one-edit fix.
			slog.Warn("adopt failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("name " + msg.name + ": " + clip(msg.err))
			return m, nil
		}
		slog.Info("adopted", "name", msg.name, "title", msg.title)
		m.adoptFor, m.adoptProject = "", ""
		m.input.SetValue("")
		named := "named it " + msg.name
		if msg.title != "" {
			named += " (" + msg.title + ")"
		}
		if m.adoptNote {
			m.noteFor = msg.name
			m.status = okStyle.Render(named + "; now its note (Enter saves, Esc skips)")
		} else {
			m.status = okStyle.Render(named)
		}
		return m, readSessionsCmd()
	case reposMsg:
		if m.mode == modeNew {
			m.nd.repos, m.nd.reposErr = msg.info, msg.err
		}
		return m, nil
	case workOutputMsg:
		if m.mode == modeOutput && msg.seq == m.out.seq {
			m.out.text = msg.text
			m.refreshOutput()
		}
		return m, nil
	case outputTick:
		if m.mode != modeOutput || msg.seq != m.out.seq {
			return m, nil
		}
		return m, tea.Batch(m.workOutputCmd(msg.seq, m.out.name), outputTickCmd(msg.seq))
	}
	return m, nil
}

// ---- the output view (Ctrl-O): what that tab's Claude said last ----------

type outputState struct {
	name   string
	seq    int // bumped on enter and leave; stale fetches and ticks are dropped
	text   string
	follow bool // stick to the bottom as new replies arrive
}

func (m *model) enterOutput(name string) tea.Cmd {
	m.mode = modeOutput
	m.out = outputState{name: name, seq: m.out.seq + 1, follow: true, text: "fetching..."}
	m.vp = viewport.New(m.width, m.outHeight())
	m.refreshOutput()
	m.status = ""
	return tea.Batch(m.workOutputCmd(m.out.seq, name), outputTickCmd(m.out.seq))
}

func (m *model) leaveOutput() {
	m.mode = modeBoard
	m.out.seq++
}

func (m model) outHeight() int {
	h := m.height - outputChrome
	if h < 3 {
		h = 3
	}
	return h
}

func (m *model) refreshOutput() {
	m.vp.Width = m.width
	m.vp.Height = m.outHeight()
	m.vp.SetContent(wrapTo(strings.ReplaceAll(m.out.text, "\r", ""), m.width))
	if m.out.follow {
		m.vp.GotoBottom()
	}
}

func (m model) updateOutputKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab:
		m.leaveOutput()
		return m, nil
	case tea.KeyUp:
		m.out.follow = false
		m.vp.ScrollUp(1)
	case tea.KeyDown:
		m.vp.ScrollDown(1)
		if m.vp.AtBottom() {
			m.out.follow = true
		}
	case tea.KeyPgUp:
		m.out.follow = false
		m.vp.PageUp()
	case tea.KeyPgDown:
		m.vp.PageDown()
		if m.vp.AtBottom() {
			m.out.follow = true
		}
	case tea.KeyHome:
		m.out.follow = false
		m.vp.GotoTop()
	case tea.KeyEnd:
		m.out.follow = true
		m.vp.GotoBottom()
	}
	return m, nil
}

func (m model) viewOutput() string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s   last %s turns, refreshed every %ds   %s\n",
		headerStyle.Render("savannah  output "+m.out.name), outputTurns,
		int(outputRefresh/time.Second), time.Now().Format("15:04:05"))
	b.WriteString(m.vp.View() + "\n")
	b.WriteString(helpStyle.Render("Up/Down scroll   End follow   Esc back   Ctrl-C quit"))
	return b.String()
}

// ---- the new-task dialog (Ctrl-N) ------------------------------------------

const (
	fieldName = iota
	fieldRepo
	fieldPrompt
	fieldWorktree
	fieldCount
)

type newDialog struct {
	name, repo, prompt textinput.Model
	worktree           bool
	focus              int
	err                string // validation or CLI error, shown under the fields
	busy               bool   // `work new` is running; Enter waits, Esc leaves
	repos              reposInfo
	reposErr           string
	home               string // for ~ in the Repo field and in hints
}

func (m *model) enterNew() tea.Cmd {
	m.mode = modeNew
	m.noteFor, m.adoptFor, m.titleFor, m.pendingRm, m.status = "", "", "", "", ""
	mk := func(placeholder string, limit int) textinput.Model {
		ti := textinput.New()
		ti.Placeholder = placeholder
		ti.CharLimit = limit
		ti.Width = 56
		return ti
	}
	m.nd = newDialog{
		name:     mk("fix-parser, or words: Fix the parser (the name is derived)", 120),
		repo:     mk("a repo from the list, scratch, or a path", 512),
		prompt:   mk("what that Claude should do first (optional)", 4000),
		worktree: true,
	}
	m.nd.home, _ = os.UserHomeDir()
	m.nd.setFocus(fieldName)
	return m.reposCmd()
}

func (d *newDialog) setFocus(i int) {
	d.focus = ((i % fieldCount) + fieldCount) % fieldCount
	d.name.Blur()
	d.repo.Blur()
	d.prompt.Blur()
	switch d.focus {
	case fieldName:
		d.name.Focus()
	case fieldRepo:
		d.repo.Focus()
	case fieldPrompt:
		d.prompt.Focus()
	}
}

func (m model) updateNewKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	d := &m.nd
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc:
		m.mode = modeBoard
		m.nd = newDialog{}
		return m, nil
	case tea.KeyTab, tea.KeyDown:
		d.setFocus(d.focus + 1)
		return m, nil
	case tea.KeyShiftTab, tea.KeyUp:
		d.setFocus(d.focus - 1)
		return m, nil
	case tea.KeyEnter:
		return m.submitNew()
	case tea.KeySpace:
		if d.focus == fieldWorktree {
			d.worktree = !d.worktree
			return m, nil
		}
	}
	d.err = "" // typing again is the fix for whatever was wrong
	var cmd tea.Cmd
	switch d.focus {
	case fieldName:
		d.name, cmd = d.name.Update(k)
	case fieldRepo:
		d.repo, cmd = d.repo.Update(k)
	case fieldPrompt:
		d.prompt, cmd = d.prompt.Update(k)
	}
	return m, cmd
}

// submitNew validates in the dialog (so a bad name never reaches git) and
// hands the rest to `work new`, whose own refusals come back into d.err.
func (m model) submitNew() (tea.Model, tea.Cmd) {
	d := &m.nd
	if d.busy {
		return m, nil
	}
	text := strings.TrimSpace(d.name.Value())
	repo := strings.TrimSpace(d.repo.Value())
	if text == "" {
		d.err = "name is required"
		d.setFocus(fieldName)
		return m, nil
	}
	name, title, nerr := nameFor(text, m.takenNames())
	switch {
	case nerr != "":
		d.err = nerr
		d.setFocus(fieldName)
		return m, nil
	case repo == "":
		d.err = "repo is required: a name from the list, scratch, or a path"
		d.setFocus(fieldRepo)
		return m, nil
	}
	args := workNewArgs(name, title, repo, strings.TrimSpace(d.prompt.Value()), d.worktree, d.repos, d.home)
	slog.Info("action", "do", "new", "name", name, "title", title, "args", args)
	d.busy, d.err = true, ""
	return m, m.workNewCmd(name, args)
}

// resolveRepo turns the Repo field into the --repo path: a listed repo's
// name (exact, else a unique prefix) becomes <dir>/<name>; "scratch" is the
// scratch directory, which has no git and so no worktree; anything else is
// a path, with a leading ~ expanded.
func resolveRepo(text string, repos reposInfo, home string) (path string, scratch bool) {
	text = strings.TrimSpace(text)
	if text == "scratch" && repos.Scratch != "" {
		return repos.Scratch, true
	}
	if repos.Dir != "" && text != "" {
		var prefixed []string
		for _, r := range repos.Repos {
			if r == text {
				return filepath.Join(repos.Dir, r), false
			}
			if strings.HasPrefix(r, text) {
				prefixed = append(prefixed, r)
			}
		}
		if len(prefixed) == 1 {
			return filepath.Join(repos.Dir, prefixed[0]), false
		}
	}
	if home != "" && (text == "~" || strings.HasPrefix(text, "~/")) {
		return filepath.Join(home, text[1:]), false
	}
	return text, false
}

// workNewArgs is the pure half of submitNew: the exact argv for `work new`.
func workNewArgs(name, title, repoText, prompt string, worktree bool, repos reposInfo, home string) []string {
	path, scratch := resolveRepo(repoText, repos, home)
	args := workArgs("new", name, "--repo="+path)
	if title != "" {
		args = append(args, "--title="+title)
	}
	if prompt != "" {
		args = append(args, "--prompt="+prompt)
	}
	if !worktree || scratch {
		args = append(args, "--no-worktree")
	}
	return append(args, "--json")
}

// tilde shortens a path under home to ~/..., for hints.
func tilde(path, home string) string {
	if home != "" && (path == home || strings.HasPrefix(path, home+string(filepath.Separator))) {
		return "~" + path[len(home):]
	}
	return path
}

// repoMatches lists the repos the typed prefix still allows (plus "scratch"
// when a scratch directory exists), for the hint under the Repo field.
func repoMatches(text string, repos reposInfo, max int) []string {
	text = strings.TrimSpace(text)
	var out []string
	for _, r := range repos.Repos {
		if strings.HasPrefix(r, text) {
			out = append(out, r)
		}
	}
	if repos.Scratch != "" && strings.HasPrefix("scratch", text) {
		out = append(out, "scratch")
	}
	if len(out) > max {
		out = out[:max]
	}
	return out
}

// fit truncates to w runes when a width is known; before the first
// WindowSizeMsg (w <= 0) it leaves the text alone.
func fit(s string, w int) string {
	if w <= 0 {
		return s
	}
	return trunc(s, w)
}

func (m model) viewNew() string {
	d := m.nd
	var b strings.Builder
	fmt.Fprintf(&b, "%s   %s\n\n", headerStyle.Render("savannah  new work session"),
		time.Now().Format("15:04:05"))
	label := func(i int, text string) string {
		s := pad(text, 10)
		if d.focus == i {
			return selectedStyle.Render(s)
		}
		return colStyle.Render(s)
	}
	b.WriteString("  " + label(fieldName, "Name") + d.name.View() + "\n")
	b.WriteString("  " + pad("", 10) + helpStyle.Render(fit(nameHint(d.name.Value()), m.width-14)) + "\n")
	b.WriteString("  " + label(fieldRepo, "Repo") + d.repo.View() + "\n")
	hint := ""
	switch {
	case d.reposErr != "":
		hint = "repos: " + d.reposErr
	case d.repos.Dir == "" && d.repos.Scratch == "":
		hint = "no folder of repos remembered (savannah work repos DIR sets one); type a path"
	default:
		ms := repoMatches(d.repo.Value(), d.repos, 8)
		where := tilde(d.repos.Dir, d.home)
		if d.repos.Picked == "cwd" {
			where += " (the cwd; savannah work repos DIR remembers a folder)"
		}
		if len(ms) == 0 {
			hint = "nothing in " + where + " matches; the text is used as a path"
		} else {
			hint = where + ": " + strings.Join(ms, "  ")
		}
	}
	b.WriteString("  " + pad("", 10) + helpStyle.Render(fit(hint, m.width-14)) + "\n")
	b.WriteString("  " + label(fieldPrompt, "Prompt") + d.prompt.View() + "\n")
	box := "[ ]"
	if d.worktree {
		box = "[x]"
	}
	b.WriteString("  " + label(fieldWorktree, "Worktree") + box +
		" fresh worktree on branch work/<name>  (Space toggles; off = run in the repo itself)\n\n")
	switch {
	case d.busy:
		b.WriteString("  opening " + strings.TrimSpace(d.name.Value()) + "...\n")
	case d.err != "":
		b.WriteString("  " + warnStyle.Render(fit(d.err, m.width-4)) + "\n")
	default:
		b.WriteString("\n")
	}
	b.WriteString("\n" + helpStyle.Render(
		"Tab/Shift-Tab fields   Space toggles worktree   Enter opens the tab   Esc cancels"))
	return b.String()
}

// nameHint is the line under the dialog's Name field: what words become.
func nameHint(text string) string {
	text = strings.TrimSpace(text)
	switch {
	case text == "" || validWorkName(text):
		return ""
	case slugName(text) == "":
		return "no name can be made from that (a name needs a letter or digit and is never t-<digits>)"
	}
	return "name " + slugName(text) + ", titled as typed"
}
