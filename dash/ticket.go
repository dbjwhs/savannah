// ticket.go: the TICKET column, the id a row's work hangs off.
//
// The owner's work is named by ticket: a checkout `zfs-DLPX-98816` beside
// the repository, a work session `dp-2673-mssql-snapsync-throughput`, a
// title "TOOL-31974 os-upgrade nightly build", a prompt mentioning PR
// #940. On a board of thirty rows the ticket is the fastest thing to scan
// for, so it gets a column of its own, filled from the first of these that
// has one:
//
//  1. the checkout's directory name (`<repo>-<ID>` beside the repository,
//     or a savannah worktree's name): the owner's own naming;
//  2. the work session's name and branch;
//  3. the title;
//  4. the last prompt and the tab's own summary (`work ask`).
//
// A ticket is `KEY-NUMBER`: two to five letters or digits starting with a
// letter, a dash, two to seven digits (DLPX-98816, DP-2702, TOOL-31974,
// CP-11610), or `PR` and a number in any of its spellings (PR #940, PR 2353,
// pr-3141), shown as PR-940. Names are slugs, so there the match ignores
// case; in free text (a title, a prompt, a summary) only an upper-case key
// counts, so a phrase like "step-10" in a prompt is not a ticket. Up to
// two tickets show, from the one source; the rest of the row says nothing
// about them. Stated limit: a key is pattern-matched, not looked up, so a
// slug like `x86-64` or `iso-8601` would read as one.

package main

import (
	"regexp"
	"strings"
)

var (
	ticketSlugRe = regexp.MustCompile(`(?i)\b([a-z][a-z0-9]{1,4})-(\d{2,7})\b`)
	ticketTextRe = regexp.MustCompile(`\b([A-Z][A-Z0-9]{1,4})-(\d{2,7})\b`)
	ticketPRRe   = regexp.MustCompile(`(?i)\bPR[ #]?#?(\d{2,7})\b`)
)

const maxTickets = 2

// ticketsIn lists the distinct tickets in text, in order, `KEY-NUMBER`
// upper-cased; slug=true matches a key in any case (a name), else only an
// upper-case key (free text). At most maxTickets.
func ticketsIn(text string, slug bool) []string {
	var out []string
	seen := map[string]bool{}
	add := func(t string) {
		if !seen[t] && len(out) < maxTickets {
			seen[t] = true
			out = append(out, t)
		}
	}
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
	for i := 1; i < len(hits); i++ {
		for j := i; j > 0 && hits[j].at < hits[j-1].at; j-- {
			hits[j], hits[j-1] = hits[j-1], hits[j]
		}
	}
	for _, h := range hits {
		add(h.id)
	}
	return out
}

// ticketSource is one place a row's ticket may be named; slug says the
// match ignores case (a name) rather than wanting an upper-case key.
type ticketSource struct {
	text string
	slug bool
}

// ticketFor is a row's TICKET cell: the tickets of the first source that
// names one (the checkout, the work session's name and branch, the title,
// the last prompt and the summary), joined by a space; "" when none.
func ticketFor(s session, rec workRecord, hasRec bool) string {
	sources := []ticketSource{
		{checkoutLabel(repoInfoOf(s.Project)), true},
		{s.Work, true},
		{rec.Branch, true},
		{s.WorkTitle, false},
		{s.LastPrompt, false},
		{rec.Summary, false},
	}
	if !hasRec {
		sources = []ticketSource{sources[0], sources[1], sources[3], sources[4]}
	}
	for _, src := range sources {
		if src.text == "" {
			continue
		}
		if t := ticketsIn(src.text, src.slug); len(t) > 0 {
			return strings.Join(t, " ")
		}
	}
	return ""
}

// fillTickets sets every row's Ticket from its sources (after joinWork
// has tagged the rows with their records).
func fillTickets(rows []session, work []workRecord) {
	recs := recordMap(work)
	for i := range rows {
		rec, has := recs[rows[i].Work]
		rows[i].Ticket = ticketFor(rows[i], rec, has)
	}
}
