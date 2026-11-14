// ticket.go: the TICKET column, the ids a row's work hangs off, and the
// links behind them (Cmd-click in iTerm2, or Ctrl-B).
//
// The owner's work is named by ticket: a checkout `zfs-DLPX-98816` beside
// the repository, a work session `dp-2673-mssql-snapsync-throughput`, a
// title "TOOL-31974 os-upgrade nightly build", a prompt mentioning PR
// #940. On a board of thirty rows the ticket is the fastest thing to scan
// for, so it gets a column of its own. Two slots: the ticket, from the
// first of these sources that names one, and the pull request, from the
// first that names one:
//
//  1. the checkout's directory name (`<repo>-<ID>` beside the repository,
//     or a savannah worktree's name): the owner's own naming;
//  2. the work session's name and branch;
//  3. the title;
//  4. the last prompt and the tab's own summary (`work ask`).
//
// A pull request named by URL (https://github.com/org/repo/pull/940 in
// the summary or the notes, which is what the default `work ask` question
// asks every tab to include) comes before one named by number, because
// the URL says which repository.
//
// A ticket is `KEY-NUMBER`: two to five letters or digits starting with a
// letter, a dash, two to seven digits (DLPX-98816, DP-2702, TOOL-31974,
// CP-11610); a pull request is `PR` and a number in any of its spellings
// (PR #940, PR 2353, pr-3141), shown as PR-940. Names are slugs, so there
// the match ignores case; in free text (a title, a prompt, a summary) only
// an upper-case key counts, so a phrase like "step-10" in a prompt is not
// a ticket. Stated limit: a key is pattern-matched, not looked up, so a
// slug like `x86-64` or `iso-8601` would read as one.
//
// Links: a ticket links to the tracker's page, the author's Jira unless
// the board file names another ("ticket_url": "https://x.atlassian.net/browse/{ticket}",
// set with `savannah --ticket-url URL`; "off" for no ticket links); a
// pull request links to its
// URL when one was seen, else to the checkout's origin remote on GitHub
// (`git remote get-url origin`, cached for the run) plus /pull/N. The link
// is an OSC 8 terminal hyperlink around the text (iTerm2: Cmd-click), and
// Ctrl-B opens the row's links with the system opener for a terminal
// that shows no hyperlinks (tmux strips them unless told otherwise).

package main

import (
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"

	tea "github.com/charmbracelet/bubbletea"
)

var (
	ticketSlugRe = regexp.MustCompile(`(?i)\b([a-z][a-z0-9]{1,4})-(\d{2,7})\b`)
	ticketTextRe = regexp.MustCompile(`\b([A-Z][A-Z0-9]{1,4})-(\d{2,7})\b`)
	ticketPRRe   = regexp.MustCompile(`(?i)\bPR[ #]?#?(\d{2,7})\b`)
	prURLRe      = regexp.MustCompile(`https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/pull/(\d+)`)
)

const maxTickets = 2

// defaultTicketURL is the tracker's page for a ticket when the board file
// says nothing: the author's Jira. Anyone else sets theirs once with
// `savannah --ticket-url URL`.
const defaultTicketURL = "https://perforce.atlassian.net/browse/{ticket}"

// ticketURLPattern is the tracker's page for a ticket, with {ticket} for
// the key; "" = no ticket links.
var ticketURLPattern = defaultTicketURL

// resolveTicketURL is the pattern the board file's value means: "" is
// the default, "off" is none, anything else is itself.
func resolveTicketURL(pref string) string {
	switch pref {
	case "":
		return defaultTicketURL
	case "off":
		return ""
	}
	return pref
}

// ticketsIn lists the distinct ids in text, in order, `KEY-NUMBER`
// upper-cased (a pull request as PR-N); slug=true matches a key in any
// case (a name), else only an upper-case key (free text). At most
// maxTickets.
func ticketsIn(text string, slug bool) []string {
	var out []string
	seen := map[string]bool{}
	type hit struct {
		at int
		id string
	}
	var hits []hit
	re := ticketTextRe
	if slug {
		re = ticketSlugRe
	}
	for _, m := range re.FindAllStringSubmatchIndex(text, -1) {
		hits = append(hits, hit{m[0], strings.ToUpper(text[m[2]:m[3]]) + "-" + text[m[4]:m[5]]})
	}
	for _, m := range ticketPRRe.FindAllStringSubmatchIndex(text, -1) {
		hits = append(hits, hit{m[0], "PR-" + text[m[2]:m[3]]})
	}
	// In text order, whichever pattern found them; "PR-3141" matches both
	// patterns at one offset and the set keeps it once.
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].at < hits[j].at })
	for _, h := range hits {
		if !seen[h.id] && len(out) < maxTickets {
			seen[h.id] = true
			out = append(out, h.id)
		}
	}
	return out
}

// prURLIn is the last pull request URL in text (the newest mention), as
// ("PR-940", the URL); ("", "") when none.
func prURLIn(text string) (id, url string) {
	ms := prURLRe.FindAllStringSubmatch(text, -1)
	if len(ms) == 0 {
		return "", ""
	}
	m := ms[len(ms)-1]
	return "PR-" + m[3], "https://github.com/" + m[1] + "/" + m[2] + "/pull/" + m[3]
}

// ticketSource is one place a row's ids may be named; slug says the match
// ignores case (a name) rather than wanting an upper-case key.
type ticketSource struct {
	text string
	slug bool
}

// ticketFor fills a row's ticket slots and their links: the ticket from
// the first source naming one, the pull request by URL first (summary,
// notes) then by number from the first source naming one.
func ticketFor(s session, rec workRecord, hasRec bool) (ticket, ticketURL, pr, prURL string) {
	info := repoInfoOf(s.Project)
	sources := []ticketSource{
		{checkoutLabel(info), true},
		{s.Work, true},
		{rec.Branch, true},
		{s.WorkTitle, false},
		{s.LastPrompt, false},
		{rec.Summary, false},
	}
	if !hasRec {
		sources = []ticketSource{sources[0], sources[1], sources[3], sources[4]}
	}
	if hasRec {
		if id, u := prURLIn(rec.Summary); id != "" {
			pr, prURL = id, u
		} else if id, u := prURLIn(rec.notes); id != "" {
			pr, prURL = id, u
		}
	}
	for _, src := range sources {
		if src.text == "" {
			continue
		}
		for _, id := range ticketsIn(src.text, src.slug) {
			if strings.HasPrefix(id, "PR-") {
				if pr == "" {
					pr = id
				}
			} else if ticket == "" {
				ticket = id
			}
		}
		if ticket != "" && pr != "" {
			break
		}
	}
	if ticket != "" && ticketURLPattern != "" {
		ticketURL = ticketLink(ticketURLPattern, ticket)
	}
	if pr != "" && prURL == "" {
		if web := repoWeb(info.checkout); web != "" {
			prURL = web + "/pull/" + strings.TrimPrefix(pr, "PR-")
		}
	}
	return ticket, ticketURL, pr, prURL
}

// ticketLink fills the pattern: {ticket} replaced, or the key appended to
// a pattern without the placeholder ("https://x.atlassian.net/browse/").
func ticketLink(pattern, ticket string) string {
	if strings.Contains(pattern, "{ticket}") {
		return strings.ReplaceAll(pattern, "{ticket}", ticket)
	}
	if strings.HasSuffix(pattern, "/") {
		return pattern + ticket
	}
	return pattern + "/" + ticket
}

// fillTickets sets every row's TICKET cell and links from its sources
// (after joinWork has tagged the rows with their records).
func fillTickets(rows []session, work []workRecord) {
	recs := recordMap(work)
	for i := range rows {
		rec, has := recs[rows[i].Work]
		ticket, tu, pr, pu := ticketFor(rows[i], rec, has)
		rows[i].Ticket = strings.TrimSpace(ticket + " " + pr)
		rows[i].TicketID, rows[i].TicketURL = ticket, tu
		rows[i].PR, rows[i].PRURL = pr, pu
	}
}

// ---- the repository's web page, from its origin remote ---------------------

var repoWebCache = struct {
	sync.Mutex
	m map[string]string
}{m: map[string]string{}}

var gitRemote = func(dir string) string {
	out, err := run("git", "-C", dir, "remote", "get-url", "origin")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// repoWeb is https://github.com/org/repo for a checkout whose origin is on
// GitHub (https, ssh or git@ form), "" otherwise. One git call per
// checkout per run.
func repoWeb(checkout string) string {
	if checkout == "" {
		return ""
	}
	repoWebCache.Lock()
	defer repoWebCache.Unlock()
	if w, ok := repoWebCache.m[checkout]; ok {
		return w
	}
	w := githubWeb(gitRemote(checkout))
	repoWebCache.m[checkout] = w
	return w
}

// githubWeb turns an origin URL into its GitHub page: git@github.com:o/r.git,
// ssh://git@github.com/o/r.git, https://github.com/o/r(.git). "" for any
// other host.
func githubWeb(remote string) string {
	remote = strings.TrimSpace(remote)
	var path string
	switch {
	case strings.HasPrefix(remote, "git@github.com:"):
		path = strings.TrimPrefix(remote, "git@github.com:")
	case strings.HasPrefix(remote, "ssh://git@github.com/"):
		path = strings.TrimPrefix(remote, "ssh://git@github.com/")
	case strings.HasPrefix(remote, "https://github.com/"):
		path = strings.TrimPrefix(remote, "https://github.com/")
	default:
		return ""
	}
	path = strings.TrimSuffix(strings.Trim(path, "/"), ".git")
	if strings.Count(path, "/") != 1 {
		return ""
	}
	return "https://github.com/" + path
}

// ---- hyperlinks -------------------------------------------------------------

// osc8 wraps text in a terminal hyperlink (OSC 8); the text keeps its
// width, so it is applied after a cell is padded.
func osc8(text, url string) string {
	return "\x1b]8;;" + url + "\x1b\\" + text + "\x1b]8;;\x1b\\"
}

// linkify puts the row's links around its ids inside a rendered cell (a
// cut id, or one without a link, stays plain text).
func linkify(cell string, s session) string {
	if s.TicketID != "" && s.TicketURL != "" {
		cell = strings.Replace(cell, s.TicketID, osc8(s.TicketID, s.TicketURL), 1)
	}
	if s.PR != "" && s.PRURL != "" {
		cell = strings.Replace(cell, s.PR, osc8(s.PR, s.PRURL), 1)
	}
	return cell
}

// ---- Ctrl-B: open the row's links -----------------------------------------------

type openMsg struct {
	what string
	err  string
}

// rowLinks is what Ctrl-B would open on a row: the ids with a link.
func rowLinks(s session) (ids, urls []string) {
	if s.TicketID != "" && s.TicketURL != "" {
		ids, urls = append(ids, s.TicketID), append(urls, s.TicketURL)
	}
	if s.PR != "" && s.PRURL != "" {
		ids, urls = append(ids, s.PR), append(urls, s.PRURL)
	}
	return ids, urls
}

// openerArgv is the system opener: `open` on macOS, `xdg-open` elsewhere.
var openerArgv = func() []string {
	if runtime.GOOS == "darwin" {
		return []string{"open"}
	}
	return []string{"xdg-open"}
}

// pressOpen is Ctrl-B on a session row: its links go to the browser, the
// status line says which (or why none).
func (m model) pressOpen() (model, tea.Cmd) {
	r, ok := m.currentRow()
	if !ok || r.kind != rowSession {
		m.status = warnStyle.Render("Ctrl-B opens a row's ticket and pull request; select a session")
		return m, nil
	}
	s := r.s
	ids, urls := rowLinks(s)
	if len(ids) == 0 {
		switch {
		case s.TicketID != "" && ticketURLPattern == "":
			m.status = warnStyle.Render("no link for " + s.TicketID + ": ticket links are off; savannah --ticket-url URL ({ticket} for the key) sets the tracker")
		case s.PR != "":
			m.status = warnStyle.Render("no link for " + s.PR + ": no GitHub origin on this checkout and no URL seen in its summary or notes")
		default:
			m.status = warnStyle.Render("no ticket on this row (Ctrl-B opens a row's ticket and pull request)")
		}
		return m, nil
	}
	m.act("open", "opening "+strings.Join(ids, " and ")+"...", "ids", strings.Join(ids, " "), "urls", strings.Join(urls, " "))
	return m, func() tea.Msg {
		for i, u := range urls {
			if _, err := runArgv(run, openerArgv(), u); err != nil {
				return openMsg{what: ids[i], err: cliError(err)}
			}
		}
		return openMsg{what: strings.Join(ids, " and ")}
	}
}

func (m model) updateOpen(msg openMsg) (tea.Model, tea.Cmd) {
	if msg.err != "" {
		m.fail("open failed", "could not open "+msg.what, msg.err, "what", msg.what)
		return m, nil
	}
	m.status = okStyle.Render("opened " + msg.what + " in the browser")
	return m, nil
}
