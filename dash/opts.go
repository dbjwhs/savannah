// opts.go: the command line, and what the board file fills in where the
// command line says nothing.

package main

import (
	"fmt"
	"os"
	"strings"
)

// opts is the command line: work board by default, mesh on request.
type opts struct {
	pin, key, addr string
	mesh, help     bool
	group          int    // --group 1, --no-group -1, 0 = as the board file remembers
	autoname       int    // --autoname 1, --no-autoname -1, 0 = on unless the board file says "off"
	fade           string // --fade DUR|off; "" = as the board file remembers, else 3h
	ticketURL      string // --ticket-url URL: the tracker's page, remembered in the board file
	history        bool   // HISTORY open at start (the board file's "history": "open")
	mouse          int    // --mouse 1, --no-mouse -1, 0 = on unless the board file says "off"
}

// parseArgs reads the argv after the program name. A leading non-flag word
// pins one node (and implies --mesh). --no-mesh and --sessions, the old way
// to get the work board, are accepted and mean nothing now. --group and
// --no-group set the grouping for this run over the remembered choice.
func parseArgs(args []string) (opts, error) {
	var o opts
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		o.pin, o.mesh = args[0], true
		args = args[1:]
	}
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--key", "--addr", "--fade", "--ticket-url":
			if i+1 >= len(args) {
				return o, fmt.Errorf("%s needs a value", args[i])
			}
			switch args[i] {
			case "--key":
				o.key = args[i+1]
			case "--addr":
				o.addr = args[i+1]
			case "--ticket-url":
				if u := args[i+1]; u != "off" && u != "default" && !strings.HasPrefix(u, "http://") && !strings.HasPrefix(u, "https://") {
					return o, fmt.Errorf("--ticket-url %q: want an http(s) URL with {ticket} for the key, off, or default", u)
				}
				o.ticketURL = args[i+1]
			default:
				if _, err := parseFade(args[i+1]); err != nil {
					return o, err
				}
				o.fade = args[i+1]
			}
			i++
		case "--mesh":
			o.mesh = true
		case "--group":
			o.group = 1
		case "--no-group":
			o.group = -1
		case "--autoname":
			o.autoname = 1
		case "--no-autoname":
			o.autoname = -1
		case "--mouse":
			o.mouse = 1
		case "--no-mouse":
			o.mouse = -1
		case "--no-mesh", "--sessions", "--work":
			// The work board is the default; kept so old scripts still run.
		case "-h", "--help":
			o.help = true
		default:
			return o, fmt.Errorf("unknown argument %q", args[i])
		}
	}
	return o, nil
}

// applyPrefs fills in, from the board file, what the command line left
// unsaid: a flag given either way wins for this run. HISTORY has no flag,
// so the file alone decides it.
func (o *opts) applyPrefs(p boardPrefs) {
	unsaid := func(flag *int, remembered bool, as int) {
		if *flag == 0 && remembered {
			*flag = as
		}
	}
	unsaid(&o.group, p.Group == "repo", 1)
	unsaid(&o.autoname, p.Autoname == "off", -1)
	unsaid(&o.mouse, p.Mouse == "off", -1)
	o.history = p.History == "open"
	if o.fade == "" {
		o.fade = p.Fade // "" stays the default; a bad value in the file is the default too
	}
}

// rememberTicketURL writes --ticket-url to the board file and returns the
// prefs as they then stand. The tracker is configuration, not a view
// toggle (there is no key on the board to set it): "off" is remembered as
// off, "default" goes back to the built-in pattern. Without the flag the
// prefs come back as given.
func (o opts) rememberTicketURL(prefs boardPrefs) boardPrefs {
	if o.ticketURL == "" {
		return prefs
	}
	prefs, err := updateBoardPrefs(func(p *boardPrefs) {
		p.TicketURL = o.ticketURL
		if o.ticketURL == "default" {
			p.TicketURL = ""
		}
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "savannah-dash: --ticket-url not remembered:", err)
	}
	return prefs
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: savannah-dash [--mesh] [<node>] [--addr H:P] [--key FILE] [--group|--no-group] [--fade DUR|off] [--ticket-url URL] [--no-autoname] [--no-mouse]")
	fmt.Fprintln(os.Stderr, "  no flags:  the work board: your work sessions and open Claude Code tabs")
	fmt.Fprintln(os.Stderr, "  --mesh:    also discover every node via `savannah ls` and show its workers")
	fmt.Fprintln(os.Stderr, "  <node>:    pin one node (use --addr for a node not on mDNS); implies --mesh")
	fmt.Fprintln(os.Stderr, "  --group:   gather the rows by repository (Ctrl-G toggles it; the choice is")
	fmt.Fprintln(os.Stderr, "             remembered in $SAVANNAH_BOARD_FILE, ~/.claude/savannah/board.json)")
	fmt.Fprintln(os.Stderr, "  --fade DUR: dim the rows untouched for longer than DUR (3h unless set; off = never;")
	fmt.Fprintln(os.Stderr, "             Ctrl-F cycles 1h, 3h, 8h, 24h, off and remembers it in the board file)")
	fmt.Fprintln(os.Stderr, "  --ticket-url URL: where a ticket links, {ticket} for the key (remembered in the")
	fmt.Fprintln(os.Stderr, "             board file; the default is "+defaultTicketURL+";")
	fmt.Fprintln(os.Stderr, "             off = no ticket links, default = back to the built-in pattern)")
	fmt.Fprintln(os.Stderr, "  --no-autoname:  do not name the tabs you opened by hand (`work autoname`;")
	fmt.Fprintln(os.Stderr, "             \"autoname\": \"off\" in the board file turns it off for good)")
	fmt.Fprintln(os.Stderr, "  --no-mouse:     no mouse reporting (click selects, double click opens, the wheel")
	fmt.Fprintln(os.Stderr, "             scrolls; with it on, select text with Option-drag); \"mouse\": \"off\" in")
	fmt.Fprintln(os.Stderr, "             the board file turns it off for good")
	fmt.Fprintln(os.Stderr, "Sessions come from the session-status hook, work sessions from $SAVANNAH_WORK_DIR")
	fmt.Fprintln(os.Stderr, "(~/.claude/savannah/work). Every action runs `savannah work ...` ($SAVANNAH_BIN).")
}
