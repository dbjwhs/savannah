// prefs.go: where savannah keeps its files, and the board file: what the
// board remembers between runs ($SAVANNAH_BOARD_FILE, default
// ~/.claude/savannah/board.json).

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// savannahPath is one of savannah's files or directories: the environment
// variable when it is set, else ~/.claude/savannah/<leaf...>; "" when
// there is no home directory to put it under.
func savannahPath(env string, leaf ...string) string {
	if v := os.Getenv(env); v != "" {
		return v
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(append([]string{home, ".claude", "savannah"}, leaf...)...)
}

// boardPrefs is $SAVANNAH_BOARD_FILE: what the board remembers between
// runs: the grouping (written by Ctrl-G) and whether the tabs you open by
// hand are named for you ("off" is written by hand, nothing on the board
// sets it). A writer loads the file first so one key never wipes another.
type boardPrefs struct {
	Group     string `json:"group"`                // "repo" | ""
	Autoname  string `json:"autoname,omitempty"`   // "off" | "" (autoname.go)
	Fade      string `json:"fade,omitempty"`       // "3h" | "off" | "" = the default (fade.go)
	TicketURL string `json:"ticket_url,omitempty"` // the tracker's page with {ticket} for the key; "off" = none; "" = the default (ticket.go)
	History   string `json:"history,omitempty"`    // "open" = HISTORY shown at start (history.go)
	Mouse     string `json:"mouse,omitempty"`      // "off" = no mouse reporting (mouse.go)
}

func boardFile() string { return savannahPath("SAVANNAH_BOARD_FILE", "board.json") }

// loadBoardPrefs reads the file; a missing or unreadable file is the zero
// value (nothing remembered), never an error the board needs to show.
func loadBoardPrefs(path string) boardPrefs {
	var p boardPrefs
	if path == "" {
		return p
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return p
	}
	_ = json.Unmarshal(b, &p)
	return p
}

// saveBoardPrefs writes the file (creating its directory). The error is
// the caller's to show: the toggle still applies to this run.
func saveBoardPrefs(path string, p boardPrefs) error {
	if path == "" {
		return fmt.Errorf("no home directory for the board file")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o644)
}

// updateBoardPrefs is one remembered change: the file is loaded, set
// changes what it means to, and the file is written back, so one key never
// wipes another. The prefs as changed come back either way.
func updateBoardPrefs(set func(*boardPrefs)) (boardPrefs, error) {
	prefs := loadBoardPrefs(boardFile())
	set(&prefs)
	return prefs, saveBoardPrefs(boardFile(), prefs)
}

// remember writes one choice to the board file for the next run. A failure
// to write is a note after the status line, not a refusal: the choice
// still holds for this run.
func (m *model) remember(set func(*boardPrefs)) {
	if _, err := updateBoardPrefs(set); err != nil {
		slog.Warn("board file", "err", err.Error())
		m.status += warnStyle.Render("; not remembered: " + err.Error())
	}
}

// groupPref is the prefs value for a grouping flag.
func groupPref(group bool) string {
	if group {
		return "repo"
	}
	return ""
}
