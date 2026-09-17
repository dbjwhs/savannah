// decision.go: the human half of the session-handoff pattern (docs/handoff.md).
//
// A worker that needs a human ends its turn with one final line:
//
//	DECISION_NEEDED {"question":"...","options":["..."],"context":"..."}
//
// and goes idle. No new wire surface exists for this on purpose: the marker
// rides the task's last_line through `task ls --json`, so a "pending
// decision" is simply a task whose state is idle or incomplete and whose
// last non-empty output line carries the marker. Answering is the ordinary
// task_send; the next turn's output replaces last_line and the flag clears
// by itself. The dash surfaces pending decisions on the board and in the
// full-screen view; this file is the detection and parsing.
//
// Stated limits: the marker is a convention with the worker's model, not an
// enforced protocol; a worker that forgets it just sits idle unflagged. Only
// the final line counts. Malformed JSON after the marker still flags the
// task, with the raw line text standing in for the question.

package main

import (
	"encoding/json"
	"strings"
)

const decisionMarker = "DECISION_NEEDED"

// A decision request parsed from a marker line.
type decision struct {
	Question string   `json:"question"`
	Options  []string `json:"options"`
	Context  string   `json:"context"`
}

// lastNonEmptyLine returns the last line of s with non-space content.
func lastNonEmptyLine(s string) string {
	lines := strings.Split(s, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if t := strings.TrimSpace(lines[i]); t != "" {
			return t
		}
	}
	return ""
}

// parseDecision reports whether line is a decision marker line and parses
// its payload. The marker must start the line; what follows must be nothing,
// or whitespace/colon then a payload. A JSON object payload fills the
// fields; anything else becomes the question verbatim (a worker that fumbles
// the JSON still gets a human's eyes, which is the whole point).
func parseDecision(line string) (decision, bool) {
	t := strings.TrimSpace(line)
	if !strings.HasPrefix(t, decisionMarker) {
		return decision{}, false
	}
	rest := t[len(decisionMarker):]
	if rest != "" && rest[0] != ' ' && rest[0] != '\t' && rest[0] != ':' &&
		rest[0] != '{' {
		return decision{}, false // DECISION_NEEDEDFOO is not a marker
	}
	rest = strings.TrimLeft(rest, " \t:")
	var d decision
	if strings.HasPrefix(rest, "{") {
		if json.Unmarshal([]byte(rest), &d) != nil {
			d = decision{Question: rest}
		}
	} else {
		d.Question = rest
	}
	if strings.TrimSpace(d.Question) == "" {
		d.Question = "(no question given)"
	}
	return d, true
}

// pendingDecision reports whether t is waiting on a human right now: the
// turn is over (idle, or incomplete after the auto-continue budget) and the
// final output line is a marker. A running task is never pending, even
// while its last_line still shows the old marker.
func pendingDecision(t task) (decision, bool) {
	if t.State != "idle" && t.State != "incomplete" {
		return decision{}, false
	}
	return parseDecision(lastNonEmptyLine(t.LastLine))
}

// countDecisions tallies pending decisions across the board.
func countDecisions(rows []row) int {
	n := 0
	for _, r := range rows {
		if _, ok := pendingDecision(r.t); ok {
			n++
		}
	}
	return n
}

// decisionCell renders the LAST LINE table cell for a pending decision.
func decisionCell(d decision) string {
	s := "DECISION: " + d.Question
	if len(d.Options) > 0 {
		s += "  [" + strings.Join(d.Options, " | ") + "]"
	}
	return s
}
