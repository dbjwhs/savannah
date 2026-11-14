// polling.go: a tab's own progress report on the board.
//
// A Claude that is waiting on something slow (tests, CI, a deploy, a build)
// can tell the board so: `savannah work status <name> -- "waiting for ctest
// (run 2 of 3)"` writes <work dir>/<name>/status.json, {"text", "updated",
// "count"}, the count going up by one per call. The board shows it as the
// row's state "polling" while the report is LIVE: the text, the poll count
// and its age in the WHAT cell, a line in the panel, and the rows so marked
// counted in the header ("N polling").
//
// Live means the session is working and the report is newer than the turn's
// prompt. The hook stamps the session's `updated` at the prompt and touches
// it again only when the turn ends (Stop) or needs you (Notification), so a
// report left over from an earlier turn is never shown as live; the panel
// still shows it, marked not live, until `work status <name> --clear`.
//
// Stated limits: the report is whatever the tab's Claude chose to write (the
// SessionStart hook tells it the command); a poll that was not reported is
// invisible. A permission prompt mid-turn flips the row to waiting, which
// hides the live marker until the turn resumes.

package main

import (
	"encoding/json"
	"fmt"
	"os"
)

// pollGlyph marks a row whose Claude has reported what it is waiting on.
const pollGlyph = "◌"

// workStatus mirrors <work dir>/<name>/status.json, written by `work status`.
type workStatus struct {
	Text    string
	Updated int64 // unix seconds of the last report
	Count   int   // reports so far: the poll count
	have    bool  // the file exists and carries a text
}

// readStatus loads a status.json. A missing, unparseable or cleared file
// (no text) is no status at all.
func readStatus(path string) workStatus {
	b, err := os.ReadFile(path)
	if err != nil {
		return workStatus{}
	}
	var raw struct {
		Text    string  `json:"text"`
		Updated float64 `json:"updated"`
		Count   int     `json:"count"`
	}
	if json.Unmarshal(b, &raw) != nil || raw.Text == "" {
		return workStatus{}
	}
	return workStatus{Text: raw.Text, Updated: int64(raw.Updated), Count: raw.Count, have: true}
}

// isPolling reports whether a session row carries a LIVE progress report:
// its Claude is working and the report is from this turn (see the file
// comment for why `updated` marks the turn's start).
func isPolling(s session, rec workRecord) bool {
	return rec.status.have && s.State == stateWorking && rec.status.Updated >= s.Updated
}

func pollsWord(n int) string {
	if n == 1 {
		return "1 poll"
	}
	return fmt.Sprintf("%d polls", n)
}

// pollingDetail is the WHAT cell of a polling row.
func pollingDetail(st workStatus, now int64) string {
	return fmt.Sprintf("polling: %s (%s, %s ago)", st.Text, pollsWord(st.Count), humanAge(now-st.Updated))
}

// statusLine is the panel's line for a report: live, or left from an
// earlier turn (kept until `work status --clear`, never shown as live).
func statusLine(st workStatus, live bool, now int64) string {
	tail := fmt.Sprintf("  (%s, %s ago", pollsWord(st.Count), humanAge(now-st.Updated))
	if live {
		return "polling  " + st.Text + tail + ")"
	}
	return "last status  " + st.Text + tail + ", not live)"
}

// countPolling counts the session rows with a live report, for the header.
func (m model) countPolling() int {
	recs := recordMap(m.work)
	n := 0
	for _, r := range m.rows {
		if r.kind != rowSession || r.s.Work == "" {
			continue
		}
		if isPolling(r.s, recs[r.s.Work]) {
			n++
		}
	}
	return n
}
