// review.go: review sessions and their section of the board (REVIEWS).
//
// A work session is for coding or for a review (the record's kind, `work
// new --mode review`, `work mode`). A review is not something the owner is
// building, so it does not belong among the rows they order by what they
// are pushing on: every review sits in one section, REVIEWS, between
// ACTIVE and LATER, whatever priority its record holds. Inside it the order
// is the owner's (Shift-Up/Down, the same ranks), and a drag stops at its
// edges; a drag between ACTIVE and LATER steps over it. A closed review is
// in HISTORY like any closed session. With no review the section is not
// drawn at all, so a board that has none looks as it always did.
//
// Ctrl-Y turns the highlighted work session into a review, or a review
// back into a coding session (`work mode <name> review|coding`), which is
// how a tab you opened yourself and named gets there. Ctrl-N makes one
// from the start: Mode is a field of the dialog, and in review mode the
// prompt field is the target (a pull request or a branch) that the CLI
// checks out and hands to Claude Code's /code-review.

package main

import (
	"fmt"
	"regexp"

	tea "github.com/charmbracelet/bubbletea"
)

const (
	kindCoding = "coding"
	kindReview = "review"
)

// isReview reports whether the record is a review; any other kind, or
// none (a record from before kinds), is coding.
func isReview(r workRecord) bool { return r.Kind == kindReview }

// reviewLines is the REVIEWS header: nothing when there is no review.
func reviewLines(n int) []string {
	if n == 0 {
		return nil
	}
	return []string{reviewStyle.Render(fmt.Sprintf("REVIEWS (%d)", n))}
}

// renumberReviews assigns ranks 10, 20, 30 ... to the ordered reviews and
// returns the records whose rank differs, each with the priority it has.
func renumberReviews(names []string, recs map[string]workRecord) []moveChange {
	var out []moveChange
	for ndx, n := range names {
		want := int64((ndx + 1) * rankStep)
		if r, ok := recs[n]; ok && effectiveRank(r) != want {
			out = append(out, moveChange{name: n, priority: priorityOf(r), rank: want})
		}
	}
	return out
}

// countReviews is the number of open reviews, shown or not.
func (m model) countReviews() int {
	return len(m.sectionNames()[secReviews])
}

// pressMode is Ctrl-Y: the highlighted work session becomes a review, or a
// review a coding session again. A plain tab has no record to hold a kind.
func (m model) pressMode() (model, tea.Cmd) {
	r, ok := m.currentRow()
	switch {
	case !ok:
		return m, nil
	case r.kind != rowSession:
		m.status = warnStyle.Render("Ctrl-Y marks a local work session as a review; a mesh worker is not one")
		return m, nil
	case !r.isWork():
		m.status = warnStyle.Render("Ctrl-Y needs a named session: Ctrl-R names this tab, then Ctrl-Y")
		return m, nil
	}
	to := kindReview
	if rec, ok := m.recordOf(r.s.Work); ok && isReview(rec) {
		to = kindCoding
	}
	m.act("mode", "making "+r.s.Work+" a "+to+" session...", "name", r.s.Work, "kind", to)
	return m, m.workVerbCmd(verbMode, r.s.Work, to)
}

var prNumberRe = regexp.MustCompile(`^#?(\d+)$`)

// reviewPR is the pull request a review record was opened on: its id as
// the TICKET column shows it and its URL when the target was one.
func reviewPR(rec workRecord) (id, url string) {
	if !isReview(rec) {
		return "", ""
	}
	if id, url := prURLIn(rec.Target); id != "" {
		return id, url
	}
	if m := prNumberRe.FindStringSubmatch(rec.Target); m != nil {
		return "PR-" + m[1], ""
	}
	return "", ""
}
