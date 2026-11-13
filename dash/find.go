// find.go: the board searched (Ctrl-K).
//
// Thirty rows, and the one you want is "the mssql throughput one" or
// "2702": Ctrl-K turns the prompt line into a filter and the board shows
// only the rows that match as you type. Every word typed must appear
// (case does not matter) somewhere in what the board already holds for a
// row: the work name, the title, the ticket and pull request, the
// project path, the branch, the last prompt, the `work ask` summary, the
// last note's first line and, for a plain tab, its iTerm2 tab title; a
// mesh worker matches on its node, id, title and worktree. A closed
// session (HISTORY) is searched too, whether the section is open or not,
// since the one you want may be the one you closed.
//
// Enter keeps the filter and gives the prompt line back, so the usual
// keys act on the row you found (Enter again opens it); Esc clears the
// filter; Ctrl-K edits it again. A drag is refused while filtered, like
// while grouped: the order is not all on the screen. Not searched, on
// purpose: the whole of a session's notes (only the highlighted row's are
// read) and the transcripts (a grep per tab).

package main

import (
	"fmt"
	"log/slog"
	"strings"
)

// filterWords is the filter as words, lower-cased; nil when blank.
func filterWords(filter string) []string {
	return strings.Fields(strings.ToLower(filter))
}

// sessionHaystack is everything a session row can be found by, lower-cased
// and space-joined.
func sessionHaystack(s session, rec workRecord, hasRec bool, titles map[string]string, home string) string {
	parts := []string{s.Work, s.WorkTitle, s.Ticket, tilde(s.Project, home), s.LastPrompt, s.State}
	if hasRec {
		parts = append(parts, rec.Branch, rec.Summary, rec.lastNote, rec.Title)
	}
	if s.Work == "" {
		parts = append(parts, titleOf(s, titles))
	}
	return strings.ToLower(strings.Join(parts, " "))
}

// rowMatches reports whether every word of the filter is in the row.
func (m model) rowMatches(r row, words []string, recs map[string]workRecord) bool {
	if len(words) == 0 {
		return true
	}
	var hay string
	if r.kind == rowSession {
		rec, has := recs[r.s.Work]
		hay = sessionHaystack(r.s, rec, has, m.titles, m.home)
	} else {
		hay = strings.ToLower(strings.Join([]string{r.node, r.t.ID, r.t.Title, r.t.Worktree, r.t.State}, " "))
	}
	for _, w := range words {
		if !strings.Contains(hay, w) {
			return false
		}
	}
	return true
}

// pressFind is Ctrl-K: the prompt line becomes the filter (prefilled with
// the one in force), any note, name or title being typed is dropped.
func (m model) pressFind() model {
	m.finding = true
	m.noteFor, m.titleFor, m.adoptFor, m.adoptProject = "", "", "", ""
	m.input.SetValue(m.filter)
	m.input.CursorEnd()
	m.status = "type to filter the rows (every word must match); Enter keeps the filter, Esc clears it"
	return m
}

// applyFilter takes the prompt line as the filter and rebuilds the rows,
// the cursor following the row it was on when that row still shows.
func (m *model) applyFilter(text string) {
	before, had := m.currentRow()
	m.filter = strings.TrimSpace(text)
	m.rebuildRows()
	if had {
		for i, r := range m.rows {
			if r.kind == before.kind && r.s.ID == before.s.ID && r.node == before.node && r.t.ID == before.t.ID {
				m.cursor = i
				return
			}
		}
	}
	m.cursor = 0
}

// acceptFind is Enter (or Ctrl-K again) while finding: the filter stays,
// the prompt line is the row's again.
func (m model) acceptFind() model {
	m.finding = false
	m.input.SetValue("")
	if m.filter == "" {
		m.status = "no filter"
		return m
	}
	slog.Info("action", "do", "find", "filter", m.filter, "rows", len(m.rows))
	verb := "match"
	if len(m.rows) == 1 {
		verb = "matches"
	}
	m.status = fmt.Sprintf("%d row%s %s %q (Esc clears the filter, Ctrl-K edits it)", len(m.rows), plural(len(m.rows)), verb, m.filter)
	return m
}

// clearFind is Esc: no filter, every row back, the cursor on the row it
// was on.
func (m model) clearFind() model {
	m.finding = false
	m.input.SetValue("")
	m.applyFilter("")
	m.status = "filter cleared"
	return m
}
