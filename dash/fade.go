// fade.go: rows you have not touched for a while are dimmed (Ctrl-F,
// --fade).
//
// Thirty tabs are thirty green rows; the ones that matter today are the
// ones with activity today. Rather than a RECENT section (rows would move
// as they are touched, and the ACTIVE/LATER order is the owner's own), a
// row whose last activity is older than the fade (3 hours unless set) is
// rendered faint in its state colour, exactly where it was. Nothing moves,
// nothing is hidden, and a row that needs you or is working is never faded
// (it has your attention by definition). Activity is the hook file's
// updated stamp: every event refreshes it, and the idle nudge lands about
// a minute after the last turn, so "last activity" is the end of the
// tab's last turn to within a minute. An offline row carries its record's
// stamp instead.
//
// Ctrl-F cycles the fade through 1h, 3h, 8h, 24h and off, and the choice
// is remembered in the board file ("fade": "8h" | "off"); --fade DUR (or
// --fade off) sets it for one run. The AGE column still says the exact
// age; the fade only decides where your eye lands first.

package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

const fadeDefault = 3 * time.Hour

// fadeSteps is what Ctrl-F cycles through; 0 is off.
var fadeSteps = []time.Duration{time.Hour, 3 * time.Hour, 8 * time.Hour, 24 * time.Hour, 0}

// parseFade reads a fade setting: "" is the default, "off" or "0" is no
// fade, else a Go duration ("3h", "90m", "1h30m"). A bare number is hours.
func parseFade(s string) (time.Duration, error) {
	s = strings.TrimSpace(strings.ToLower(s))
	switch s {
	case "":
		return fadeDefault, nil
	case "off", "0", "none":
		return 0, nil
	}
	if d, err := time.ParseDuration(s); err == nil {
		if d < 0 {
			return 0, fmt.Errorf("fade %q: a negative duration", s)
		}
		return d, nil
	}
	var h float64
	if _, err := fmt.Sscanf(s, "%g", &h); err == nil && fmt.Sprintf("%g", h) == s && h >= 0 {
		return time.Duration(h * float64(time.Hour)), nil
	}
	return 0, fmt.Errorf("fade %q: want a duration like 3h or 90m, or off", s)
}

// fadeLabel is the setting as the board file and the status line say it.
func fadeLabel(d time.Duration) string {
	if d <= 0 {
		return "off"
	}
	if d%time.Hour == 0 {
		return fmt.Sprintf("%dh", int(d/time.Hour))
	}
	return d.String()
}

// faded reports whether a row is past the fade: no fade set, a row that
// needs you, a working one (polling included) and a row with no stamp are
// never faded.
func faded(s session, now int64, fade time.Duration) bool {
	if fade <= 0 || s.Updated <= 0 || sessionAttention(s) || s.State == stateWorking {
		return false
	}
	return now-s.Updated > int64(fade/time.Second)
}

// rowStyleAt is rowStyle with the fade applied: the same colour, faint.
func rowStyleAt(s session, now int64, fade time.Duration) lipgloss.Style {
	st := rowStyle(s)
	if faded(s, now, fade) {
		return st.Faint(true)
	}
	return st
}

// nextFade is the step after d in fadeSteps, off after the last and the
// first after off; a value off the list (--fade 90m) goes to the first
// step above it, or off when none is.
func nextFade(d time.Duration) time.Duration {
	for i, step := range fadeSteps {
		if step == d {
			return fadeSteps[(i+1)%len(fadeSteps)]
		}
	}
	for _, step := range fadeSteps {
		if step > d {
			return step
		}
	}
	return 0
}

// toggleFade is Ctrl-F: the next step, said on the status line, written
// to the board file for the next run (a failure to write is a status line,
// not a refusal).
func (m model) toggleFade() model {
	m.fade = nextFade(m.fade)
	status := fmt.Sprintf("no row is dimmed for its age (Ctrl-F: %s)", fadeLabel(nextFade(m.fade)))
	if m.fade > 0 {
		status = fmt.Sprintf("rows untouched for over %s are dimmed (Ctrl-F: %s)", fadeLabel(m.fade), fadeLabel(nextFade(m.fade)))
	}
	m.act("fade", status, "fade", fadeLabel(m.fade))
	m.remember(func(p *boardPrefs) { p.Fade = fadeLabel(m.fade) })
	return m
}
