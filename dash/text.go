// text.go: the board's text helpers. Cells and lines are measured in runes
// (double-width glyphs are a stated limit of the screens that use these):
// flatten, pad, cut, clip and fit, plus the clock every screen header shows.

package main

import (
	"strings"
	"time"
	"unicode/utf8"
)

// runeLen is the length of s in runes, the unit every width here is in.
func runeLen(s string) int { return utf8.RuneCountInString(s) }

// clock is the time of day in a screen's header.
func clock() string { return time.Now().Format("15:04:05") }

// stripCR drops carriage returns, which a viewport or a wrapped column
// would otherwise draw as a jump back to the line's start.
func stripCR(s string) string { return strings.ReplaceAll(s, "\r", "") }

func flatten(s string) string {
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' || r == '\t' {
			return ' '
		}
		return r
	}, s)
}

func pad(s string, w int) string {
	r := []rune(flatten(s))
	if len(r) > w {
		r = r[:w]
	}
	return string(r) + strings.Repeat(" ", w-len(r)) + "  "
}

func trunc(s string, w int) string {
	r := []rune(flatten(s))
	if len(r) > w {
		return string(r[:w])
	}
	return string(r)
}

// fit truncates to w runes when a width is known; before the first
// WindowSizeMsg (w <= 0) it leaves the text alone. (trunc itself cuts to
// nothing at w == 0, which the panel's head line relies on.)
func fit(s string, w int) string {
	if w <= 0 {
		return s
	}
	return trunc(s, w)
}

// cutTilde keeps s within w runes, the last one a "~" when anything was
// cut, so a cut cell is never mistaken for a whole one.
func cutTilde(s string, w int) string {
	r := []rune(s)
	if w < 1 || len(r) <= w {
		return s
	}
	return string(r[:w-1]) + "~"
}

// clip keeps a CLI error to one status line: first line, capped.
func clip(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return cutTilde(s, statusClip)
}

// head is the first n runes of an output, flattened, for a VERBOSE record;
// a "~" follows when more was cut.
func head(b []byte, n int) string {
	s := flatten(strings.TrimSpace(string(b)))
	if runeLen(s) > n {
		return cutTilde(s, n+1)
	}
	return s
}

// fitHelp joins the hints with two spaces, dropping hints from the end
// while the line is wider than the terminal; the first hint always stays
// (cut if even that does not fit).
func fitHelp(parts []string, width int) string {
	for len(parts) > 1 && runeLen(strings.Join(parts, "  ")) > width {
		parts = parts[:len(parts)-1]
	}
	return trunc(strings.Join(parts, "  "), width)
}

// fitLabel truncates a footer label so label + 3 spaces + hint fits the
// terminal width (a 64-char work name would otherwise soft-wrap at 80 cols).
func fitLabel(label string, width int, hint string) string {
	if width <= 0 {
		return label
	}
	return trunc(label, max(width-3-runeLen(hint), 8))
}

// visibleLen counts runes outside ANSI escape sequences.
func visibleLen(s string) int {
	n, in := 0, false
	for _, r := range s {
		switch {
		case in:
			if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') {
				in = false
			}
		case r == '\x1b':
			in = true
		default:
			n++
		}
	}
	return n
}

// padTo pads (or truncates) a line to exactly w visible runes. A styled
// line (note stamps, titles) is only padded, never cut: its builders keep
// it within w themselves, since cutting would tear the escape codes.
func padTo(s string, w int) string {
	if strings.Contains(s, "\x1b") {
		if n := visibleLen(s); n < w {
			return s + strings.Repeat(" ", w-n)
		}
		return s
	}
	r := []rune(s)
	if len(r) > w {
		return string(r[:w])
	}
	return string(r) + strings.Repeat(" ", w-len(r))
}
