// logging.go: the board's half of savannah's one log.
//
// Every local piece (this board, the work CLI, the hook, the shim) appends
// to the same file, <log dir>/savannah.log ($SAVANNAH_LOG_DIR, default
// ~/.claude/savannah/log), one line per record in log/slog's text form:
//
//	time=2026-11-05T09:12:33.123-08:00 level=INFO msg="said to demo" src=dash name=demo
//
// The file is always VERBOSE: every record is written, whatever its level,
// and the level stamp on each line is what a reader filters on (the Ctrl-L
// view here, `savannah work log` in the shell): normal = INFO and above,
// debug = DEBUG and above, verbose = everything. Levels: VERBOSE (payloads,
// output heads), DEBUG (every subprocess with argv, exit and timing; row
// changes), INFO (what you did and what came of it), WARN (a refused or
// failed command), ERROR (the board itself broke). Rotation at 10 MiB,
// three old files kept (savannah.log.1 .. .3), the same scheme Python's
// RotatingFileHandler uses on the CLI side, so either writer may rotate;
// this writer reopens when the file under its path has been swapped.
//
// Stated limits: two processes rotating at the same instant can lose a few
// lines; records are written on a best-effort basis and never block the
// board (a log directory that cannot be written means no log, not no
// board).

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	LevelVerbose = slog.Level(-8) // below slog.LevelDebug
	logMaxBytes  = 10 << 20
	logKeep      = 3
	logHeadRunes = 300 // of a subprocess's output, at VERBOSE
)

func logDir() string { return savannahPath("SAVANNAH_LOG_DIR", "log") }

func logPath() string {
	if d := logDir(); d != "" {
		return filepath.Join(d, "savannah.log")
	}
	return ""
}

// levelName is the stamp on a line; slog would print "DEBUG-4" for VERBOSE.
func levelName(l slog.Level) string {
	if l <= LevelVerbose {
		return "VERBOSE"
	}
	return l.String()
}

// levelFromName is the reader's side: a stamp back to a level, false for a
// line that carries none.
func levelFromName(s string) (slog.Level, bool) {
	switch strings.ToUpper(s) {
	case "VERBOSE", "TRACE":
		return LevelVerbose, true
	case "DEBUG":
		return slog.LevelDebug, true
	case "INFO":
		return slog.LevelInfo, true
	case "WARN", "WARNING":
		return slog.LevelWarn, true
	case "ERROR":
		return slog.LevelError, true
	}
	return 0, false
}

// logWriter appends to the log path, rotating at logMaxBytes and reopening
// when another writer rotated the file away from under it.
type logWriter struct {
	mu      sync.Mutex
	path    string
	f       *os.File
	n       int // writes since the last stat
	checked time.Time
}

func (w *logWriter) open() error {
	rotate(w.path)
	f, err := os.OpenFile(w.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if w.f != nil {
		w.f.Close()
	}
	w.f, w.n, w.checked = f, 0, time.Now()
	return nil
}

// swapped reports whether the path no longer names the open file (rotated or
// removed by someone else) or has grown past the limit.
func (w *logWriter) swapped() bool {
	st, err := os.Stat(w.path)
	if err != nil {
		return true
	}
	fi, err := w.f.Stat()
	if err != nil || !os.SameFile(st, fi) {
		return true
	}
	return st.Size() >= logMaxBytes
}

func (w *logWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil {
		if err := w.open(); err != nil {
			return len(p), nil // best effort: never fail the caller
		}
	}
	w.n++
	if w.n >= 200 || time.Since(w.checked) > 10*time.Second {
		w.n, w.checked = 0, time.Now()
		if w.swapped() {
			_ = w.open()
		}
	}
	if _, err := w.f.Write(p); err != nil {
		_ = w.open()
	}
	return len(p), nil
}

// rotate shifts savannah.log to .1 (and .1 to .2, .2 to .3) once it is at
// the size limit; a missing file is nothing to rotate.
func rotate(path string) {
	st, err := os.Stat(path)
	if err != nil || st.Size() < logMaxBytes {
		return
	}
	for i := logKeep; i >= 1; i-- {
		older := fmt.Sprintf("%s.%d", path, i)
		if i == logKeep {
			os.Remove(older)
			continue
		}
		os.Rename(older, fmt.Sprintf("%s.%d", path, i+1))
	}
	os.Rename(path, path+".1")
}

// openLog installs the board's logger as slog's default: the text handler
// at VERBOSE over the shared file, with "src=dash" on every record. With
// no usable log directory it installs a logger that drops everything.
func openLog() *slog.Logger {
	path := logPath()
	var w io.Writer = io.Discard
	if path != "" && os.MkdirAll(filepath.Dir(path), 0o755) == nil {
		w = &logWriter{path: path}
	}
	h := slog.NewTextHandler(w, &slog.HandlerOptions{
		Level: LevelVerbose,
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
			switch a.Key {
			case slog.LevelKey:
				if l, ok := a.Value.Any().(slog.Level); ok {
					a.Value = slog.StringValue(levelName(l))
				}
			case slog.TimeKey:
				a.Value = slog.StringValue(a.Value.Time().Format("2006-01-02T15:04:05.000Z07:00"))
			}
			return a
		},
	})
	l := slog.New(h).With("src", "dash")
	slog.SetDefault(l)
	return l
}

func verbose(msg string, args ...any) {
	slog.Log(context.Background(), LevelVerbose, msg, args...)
}

// run executes one subprocess for the board and logs it: DEBUG the argv,
// exit and timing (WARN when it failed, with its stderr), VERBOSE the head
// of its output. Every `savannah work ...` and `it2 ...` the board runs goes
// through here or poll, so the log says exactly what the board did and when.
func run(bin string, args ...string) ([]byte, error) {
	return runAt(slog.LevelDebug, bin, args...)
}

// poll is run for the board's periodic reads (tab titles every 5 s, mesh
// task lists every second, the output view's refetch): a success is VERBOSE
// rather than DEBUG, so the debug level stays readable; a failure is WARN.
func poll(bin string, args ...string) ([]byte, error) {
	return runAt(LevelVerbose, bin, args...)
}

func runAt(level slog.Level, bin string, args ...string) ([]byte, error) {
	start := time.Now()
	out, err := exec.Command(bin, args...).Output()
	ms := time.Since(start).Milliseconds()
	if err != nil {
		slog.Warn("command failed", "cmd", bin, "args", args, "ms", ms, "err", cliError(err))
	} else {
		slog.Log(context.Background(), level, "command", "cmd", bin, "args", args, "ms", ms, "bytes", len(out))
	}
	if len(out) > 0 {
		verbose("output", "cmd", filepath.Base(bin), "args", args, "head", head(out, logHeadRunes))
	}
	return out, err
}

// runArgv is runner (run or poll) for a command that is itself several
// words ($SAVANNAH_IT2, the system opener), the arguments after them.
func runArgv(runner func(string, ...string) ([]byte, error), argv []string, args ...string) ([]byte, error) {
	return runner(argv[0], append(append([]string{}, argv[1:]...), args...)...)
}

// runJSON runs one command through runner (run, or poll for a periodic
// read) and decodes its stdout as JSON. errText is "" on success, the
// CLI's own error when the command failed, and noJSON ("work new printed
// no record") when its output did not parse; with noJSON "" an output that
// does not parse is no error, and v is the zero value.
func runJSON[T any](runner func(string, ...string) ([]byte, error), bin string, args []string, noJSON string) (v T, errText string) {
	out, err := runner(bin, args...)
	if err != nil {
		return v, cliError(err)
	}
	if json.Unmarshal(out, &v) != nil {
		var zero T
		return zero, noJSON
	}
	return v, ""
}
