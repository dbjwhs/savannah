package main

import (
	"reflect"
	"testing"
)

func TestLastNonEmptyLine(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", ""},
		{"one", "one"},
		{"one\ntwo", "two"},
		{"one\ntwo\n", "two"},
		{"one\ntwo\n   \n\t\n", "two"},
		{"  padded  ", "padded"},
	}
	for _, c := range cases {
		if got := lastNonEmptyLine(c.in); got != c.want {
			t.Errorf("lastNonEmptyLine(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestParseDecision(t *testing.T) {
	// Well-formed JSON payload.
	d, ok := parseDecision(
		`DECISION_NEEDED {"question":"ship it?","options":["yes","no"],"context":"tests green"}`)
	if !ok || d.Question != "ship it?" || d.Context != "tests green" ||
		!reflect.DeepEqual(d.Options, []string{"yes", "no"}) {
		t.Errorf("full payload: got %+v ok=%v", d, ok)
	}

	// Question only; colon separator; surrounding whitespace.
	d, ok = parseDecision(`  DECISION_NEEDED: {"question":"which db?"}  `)
	if !ok || d.Question != "which db?" || len(d.Options) != 0 {
		t.Errorf("colon form: got %+v ok=%v", d, ok)
	}

	// Bare marker: flagged, placeholder question.
	d, ok = parseDecision("DECISION_NEEDED")
	if !ok || d.Question != "(no question given)" {
		t.Errorf("bare marker: got %+v ok=%v", d, ok)
	}

	// Plain-text payload (no JSON): the text is the question.
	d, ok = parseDecision("DECISION_NEEDED should I force-push?")
	if !ok || d.Question != "should I force-push?" {
		t.Errorf("plain text: got %+v ok=%v", d, ok)
	}

	// Malformed JSON: still flagged, raw text stands in for the question.
	d, ok = parseDecision(`DECISION_NEEDED {"question": broken`)
	if !ok || d.Question != `{"question": broken` {
		t.Errorf("malformed json: got %+v ok=%v", d, ok)
	}

	// JSON object with empty question: placeholder.
	d, ok = parseDecision(`DECISION_NEEDED {"options":["a"]}`)
	if !ok || d.Question != "(no question given)" {
		t.Errorf("empty question: got %+v ok=%v", d, ok)
	}

	// A longer identifier is not the marker.
	if _, ok = parseDecision("DECISION_NEEDEDFOO bar"); ok {
		t.Error("DECISION_NEEDEDFOO parsed as a marker")
	}
	// An unrelated line is not a marker.
	if _, ok = parseDecision("all tests passed"); ok {
		t.Error("plain line parsed as a marker")
	}
}

func TestPendingDecision(t *testing.T) {
	marker := `DECISION_NEEDED {"question":"deploy?"}`

	// Idle with the marker as the final line of multi-line output: pending.
	d, ok := pendingDecision(task{
		State: "idle", LastLine: "step 1 done\nstep 2 done\n" + marker + "\n"})
	if !ok || d.Question != "deploy?" {
		t.Errorf("idle+marker: got %+v ok=%v", d, ok)
	}

	// Incomplete (auto-continue budget spent) also counts.
	if _, ok = pendingDecision(task{State: "incomplete", LastLine: marker}); !ok {
		t.Error("incomplete+marker not pending")
	}

	// Running never counts, even with a stale marker still in last_line.
	if _, ok = pendingDecision(task{State: "running", LastLine: marker}); ok {
		t.Error("running counted as pending")
	}

	// Idle without a marker: not pending.
	if _, ok = pendingDecision(task{State: "idle", LastLine: "done"}); ok {
		t.Error("idle without marker counted as pending")
	}

	// Marker mid-output but not the final line: not pending.
	if _, ok = pendingDecision(task{
		State: "idle", LastLine: marker + "\nnever mind, resolved it"}); ok {
		t.Error("mid-output marker counted as pending")
	}

	// Failed/cancelled states never count.
	for _, st := range []string{"failed", "cancelled"} {
		if _, ok = pendingDecision(task{State: st, LastLine: marker}); ok {
			t.Errorf("%s counted as pending", st)
		}
	}
}

func TestDecisionCellAndCount(t *testing.T) {
	cell := decisionCell(decision{Question: "ship?", Options: []string{"yes", "no"}})
	if cell != "DECISION: ship?  [yes | no]" {
		t.Errorf("cell with options: %q", cell)
	}
	if got := decisionCell(decision{Question: "ship?"}); got != "DECISION: ship?" {
		t.Errorf("cell without options: %q", got)
	}

	marker := `DECISION_NEEDED {"question":"q"}`
	rows := []row{
		{node: "a", t: task{State: "idle", LastLine: marker}},
		{node: "a", t: task{State: "running", LastLine: marker}},
		{node: "b", t: task{State: "incomplete", LastLine: "x\n" + marker}},
		{node: "b", t: task{State: "idle", LastLine: "fine"}},
	}
	if n := countDecisions(rows); n != 2 {
		t.Errorf("countDecisions = %d, want 2", n)
	}
}
