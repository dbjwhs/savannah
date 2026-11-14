// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// savannah — the human's remote control (and the test harness).
//
//   savannah                       the work board (execs savannah-dash)
//   savannah mesh [...]            the board with the mesh workers too
//   savannah hub [DIR] [--no-launch]
//   savannah work <verb> ...       the local work-session CLI
//   savannah ls [--timeout-ms N]
//   savannah ask <node> "<prompt>" [options]
//   savannah info <node> [options]
//   savannah status <node> [options]
//   savannah task ...
//
// A bare `savannah` is the front door: it execs the Go dash (path baked in at
// build time as SAVANNAH_DASH_BIN, default <repo>/dash/savannah-dash, env
// overrides, `savannah-dash` on PATH as the last resort) in its default work
// mode, the board of your work sessions; `savannah mesh` is the same board
// with the mesh workers. `hub` and `work` exec python3 on
// work/savannah_work.py (SAVANNAH_WORK_PY, same scheme). None of these touch
// song; the exec happens before any mesh code runs. SAVANNAH_BIN is set to
// this binary's own path for the dash when it is not already set.
//
// <node> "local" spawns ./savannahd over pipes (Phase 1 path, no network).
// Any other <node> is a mesh name: resolved via mDNS (_agent-song._tcp), or
// taken from --addr host:port to skip discovery. --key FILE enables HMAC and
// is required to talk to any keyed node (which is every --mdns node).
//
// Output contract: TEXT chunks -> stdout verbatim; TOOL_EVENT -> stderr as
// "[tool] ..."; the RESULT trailer -> stderr as "[result] ...". Exit code 0
// iff the trailer has is_error:false. Scripts can pipe stdout cleanly.

#include <song/song.hpp>
#include <song/security.hpp>

#include <algorithm>
#include <cerrno>
#include <climits>
#include <csignal>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iostream>
#include <iterator>
#include <memory>
#include <set>
#include <string>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

#include "savannah.hpp"
#include "agent_process.hpp"  // to_cargv
#include "config.hpp"         // load_hmac_key, kDefaultAgentTimeoutMs
#include "json.hpp"           // --json output
#include "stream_json.hpp"    // ChunkKind, result_ok
#include "wire_ids.hpp"       // mesh mDNS type

// No `using namespace song`: the project namespace is `savannah` and the
// generated wire namespace is `song::savannah` (same rule as savannahd.cpp).
namespace sw = song::savannah;
using song::ServiceConnection;
using song::u16;
using song::u32;

namespace {

// How long `savannah ls` browses the mesh when --timeout-ms is not given.
constexpr u32 kBrowseTimeoutMs = 2000;
// How long resolving one node name over mDNS may take.
constexpr std::chrono::milliseconds kResolveTimeout{3000};
// TCP connect to a resolved or --addr node.
constexpr int kConnectTimeoutMs = 5000;
// Added to the agent's own timeout to make the client's per-chunk budget:
// savannahd guarantees a RESULT trailer within the agent timeout, the margin
// covers the wire and the daemon's teardown.
constexpr u32 kStreamMarginMs = 30000;

int usage() {
    std::cerr <<
        "usage: savannah [--group] [--fade DUR] [--no-mouse] the work board (your Claude tabs; needs dash/savannah-dash built)\n"
        "       savannah mesh [--key FILE]  the board with the mesh workers too\n"
        "       savannah hub [DIR] [--no-launch] [--json]\n"
        "       savannah work <new|ls|open|repos|note|notes|say|sh|jump|output|rm> ...\n"
        "       savannah ls [--timeout-ms N]\n"
        "       savannah <ask|info|status> <node> [\"prompt\"]\n"
        "       savannah task new    <node> --title T [--prompt P]\n"
        "                                   [--worktree] [--workdir DIR]\n"
        "       savannah task ls     <node> [--watch] [--json]\n"
        "       savannah task status <node> <id>\n"
        "       savannah task send   <node> <id> \"prompt\"\n"
        "       savannah task tail   <node> <id>\n"
        "       savannah task cancel <node> <id>\n"
        "       savannah task rm     <node> <id>\n"
        "               [--config PATH] [--timeout-ms N]\n"
        "               [--addr HOST:PORT] [--key FILE]\n"
        "\"local\" spawns ./savannahd over pipes; any other node is resolved\n"
        "via mDNS (or --addr) and reached over TCP, HMAC'd when --key is set.\n";
    return 2;
}

struct Cli;

// One `savannah task <sub>`: its name, the positionals it takes after the
// node, and what runs it. kTaskVerbs below is the only list of them.
struct TaskVerb {
    std::string_view name;
    bool needs_id;      // a positional task id follows the node
    bool needs_prompt;  // ... and a positional prompt follows the id
    int (*run)(sw::AgentNodeProxy&, const Cli&);
};

// One `savannah <verb> <node>`; kNodeVerbs below is the only list of them.
struct NodeVerb {
    std::string_view name;
    bool needs_prompt;  // a positional prompt follows the node
    int (*run)(sw::AgentNodeProxy&, const Cli&);
};

struct Cli {
    std::string cmd;
    std::string node;
    std::string prompt;
    std::string config;
    std::string addr;
    std::string key_file;
    u32 timeout_ms = 0;

    const NodeVerb* verb = nullptr;  // set for ask | info | status

    // `task` subcommands: `savannah task <sub> <node> [id] [prompt] [flags]`
    const TaskVerb* task = nullptr;  // set when cmd is "task"
    std::string task_id;
    std::string task_title;    // --title (task new)
    std::string task_workdir;  // --workdir (task new)
    bool task_worktree = false;  // --worktree (task new)
    bool task_watch = false;     // --watch (task ls): poll + redraw
    bool json_out = false;       // --json (task ls): machine-readable output
};

#ifndef SAVANNAH_WORK_PY
#define SAVANNAH_WORK_PY ""
#endif

std::string work_module() {
    if (const char* e = std::getenv("SAVANNAH_WORK_PY"); e && *e) return e;
    return SAVANNAH_WORK_PY;
}

#ifndef SAVANNAH_DASH_BIN
#define SAVANNAH_DASH_BIN ""
#endif

// Replace this process with argv (argv[0] looked up on PATH). Returns only
// when the exec failed, with errno set.
void exec_argv(const std::vector<std::string>& argv) {
    std::vector<char*> cargv = savannah::to_cargv(argv);
    execvp(cargv[0], cargv.data());
}

// argv[from..] as strings: the arguments handed on to the dash or the work
// module untouched.
std::vector<std::string> tail(int argc, char** argv, int from) {
    std::vector<std::string> rest;
    for (int i = from; i < argc; ++i) rest.emplace_back(argv[i]);
    return rest;
}

// Replace this process with the dash. Returns only on failure (exit 1 with
// the build hint on stderr). SAVANNAH_BIN tells the dash where this binary
// is, so `savannah work ...` from the board works even off PATH.
int exec_dash(const char* self, const std::vector<std::string>& args) {
    std::string bin;
    if (const char* e = std::getenv("SAVANNAH_DASH_BIN"); e && *e) bin = e;
    else bin = SAVANNAH_DASH_BIN;
    if (bin.empty() || access(bin.c_str(), X_OK) != 0) {
        bin = "savannah-dash";  // last resort: on PATH
    }
    if (!std::getenv("SAVANNAH_BIN") && self && std::strchr(self, '/')) {
        char real[PATH_MAX];
        if (realpath(self, real)) setenv("SAVANNAH_BIN", real, 1);
    }
    std::vector<std::string> full = {bin};
    full.insert(full.end(), args.begin(), args.end());
    exec_argv(full);
    std::cerr << "savannah: cannot run the dash (" << bin << ": " << std::strerror(errno)
              << "); build it with `cd dash && go build -o savannah-dash .` "
                 "(or `cmake --build build --target dash`), or set SAVANNAH_DASH_BIN\n";
    return 1;
}

// Replace this process with `python3 <work module> args...`. Returns only on
// failure (exit code 1 with a reason on stderr).
int exec_work(const std::vector<std::string>& args) {
    const std::string mod = work_module();
    if (mod.empty() || access(mod.c_str(), R_OK) != 0) {
        std::cerr << "savannah: work module not found (" << mod
                  << "); set SAVANNAH_WORK_PY\n";
        return 1;
    }
    std::vector<std::string> full = {"python3", mod};
    full.insert(full.end(), args.begin(), args.end());
    exec_argv(full);
    std::cerr << "savannah: cannot exec python3: " << std::strerror(errno) << "\n";
    return 1;
}

// A whole non-negative decimal number no larger than max, or false: a flag
// value like "abc" is a usage error, never a silent 0.
bool parse_number(const char* text, unsigned long max, unsigned long& out) {
    if (text == nullptr || *text < '0' || *text > '9') return false;
    char* end = nullptr;
    errno = 0;
    const unsigned long n = std::strtoul(text, &end, 10);
    if (errno != 0 || *end != '\0' || n > max) return false;
    out = n;
    return true;
}

// The flags that take a value and store it as given. Every verb accepts
// every flag; one that means nothing to a verb is ignored by it.
struct ValueFlag {
    const char* flag;
    std::string Cli::*field;
};
constexpr ValueFlag kValueFlags[] = {
    {"--config", &Cli::config},
    {"--addr", &Cli::addr},
    {"--key", &Cli::key_file},
    {"--title", &Cli::task_title},
    {"--prompt", &Cli::prompt},
    {"--workdir", &Cli::task_workdir},
};

// The flags that take no value.
struct SwitchFlag {
    const char* flag;
    bool Cli::*field;
};
constexpr SwitchFlag kSwitchFlags[] = {
    {"--worktree", &Cli::task_worktree},
    {"--watch", &Cli::task_watch},
    {"--json", &Cli::json_out},
};

// Parse the flags in argv[i..] into c. False on an unknown flag, a stray
// positional, a flag missing its value, or a --timeout-ms that is no number.
bool parse_flags(int argc, char** argv, int i, Cli& c) {
    for (; i < argc; ++i) {
        const std::string_view arg = argv[i];
        const bool has_value = i + 1 < argc;
        if (arg == "--timeout-ms" && has_value) {
            unsigned long ms = 0;
            if (!parse_number(argv[++i], 0xFFFFFFFFul, ms)) return false;
            c.timeout_ms = static_cast<u32>(ms);
            continue;
        }
        if (const auto* v = std::ranges::find(kValueFlags, arg, &ValueFlag::flag);
            v != std::end(kValueFlags) && has_value) {
            c.*(v->field) = argv[++i];
            continue;
        }
        if (const auto* sw_flag =
                std::ranges::find(kSwitchFlags, arg, &SwitchFlag::flag);
            sw_flag != std::end(kSwitchFlags)) {
            c.*(sw_flag->field) = true;
            continue;
        }
        return false;
    }
    return true;
}

// The entry named `name` in a verb table, or null.
template <typename Verb, std::size_t N>
const Verb* find_verb(const Verb (&table)[N], std::string_view name) {
    const auto* it = std::ranges::find(table, name, &Verb::name);
    return it != std::end(table) ? it : nullptr;
}

int run_ls(const Cli& cli) {
    auto discovery = song::create_discovery();
    if (!discovery || !discovery->is_available()) {
        std::cerr << "savannah: mDNS not available on this machine\n";
        return 1;
    }
    auto timeout =
        std::chrono::milliseconds(cli.timeout_ms ? cli.timeout_ms
                                                 : kBrowseTimeoutMs);
    auto found =
        discovery->discover(savannah_wire::kMeshServiceType, timeout);
    if (found.empty()) {
        std::cerr << "savannah: no nodes on the mesh\n";
        return 0;
    }
    // mDNS reports a service once per interface; collapse duplicates.
    std::set<std::string> seen;
    for (const auto& s : found) {
        std::string row =
            s.name + " " + s.host + ":" + std::to_string(s.port);
        if (seen.insert(row).second) std::cout << row << "\n";
    }
    return 0;
}

// Resolve + connect to a remote node: --addr wins, else mDNS by name.
ServiceConnection connect_remote(const Cli& cli) {
    std::string host;
    u16 port = 0;
    if (!cli.addr.empty()) {
        auto colon = cli.addr.rfind(':');
        if (colon == std::string::npos) {
            throw std::runtime_error("--addr wants HOST:PORT");
        }
        host = cli.addr.substr(0, colon);
        unsigned long n = 0;
        if (!parse_number(cli.addr.c_str() + colon + 1, 65535, n) || n == 0) {
            throw std::runtime_error("--addr wants HOST:PORT, PORT 1 to 65535");
        }
        port = static_cast<u16>(n);
    } else {
        auto discovery = song::create_discovery();
        if (!discovery || !discovery->is_available()) {
            throw std::runtime_error(
                "mDNS not available; use --addr HOST:PORT");
        }
        auto s = discovery->discover_one(
            cli.node, savannah_wire::kMeshServiceType, kResolveTimeout);
        if (!s) {
            throw std::runtime_error("node \"" + cli.node +
                                     "\" not found on the mesh");
        }
        host = s->host;
        port = s->port;
    }

    auto tcp = std::make_unique<song::TcpTransport>();
    tcp->connect(host, port, kConnectTimeoutMs);
    std::unique_ptr<song::Transport> transport = std::move(tcp);
    if (!cli.key_file.empty()) {
        transport = std::make_unique<song::SecureTransport>(
            std::move(transport),
            song::SecurityConfig(savannah::load_hmac_key(cli.key_file)));
    }
    ServiceConnection conn(std::move(transport));
    conn.init_handshake();
    return conn;
}

// Shared chunk sink for ask and task tail (same stream contract): TEXT ->
// stdout verbatim, TOOL_EVENT/RESULT -> stderr. Sets ok from the RESULT trailer.
void print_chunk(const sw::AgentChunk& c, bool& ok) {
    // The kind travels as its integer value (the IDL pins 0/1/2).
    switch (static_cast<savannah::ChunkKind>(c.kind)) {
        case savannah::ChunkKind::Text:
            std::cout << c.payload;
            std::cout.flush();
            break;
        case savannah::ChunkKind::ToolEvent:
            std::cerr << "[tool] " << c.payload << "\n";
            break;
        case savannah::ChunkKind::Result:
            std::cerr << "\n[result] " << c.payload << "\n";
            ok = savannah::ChunkMapper::result_ok(c.payload);
            break;
        default:
            std::cerr << "[?] unknown chunk kind " << c.kind << "\n";
            break;
    }
}

// The per-chunk receive budget for a stream (ask, task tail). Sized to the
// AGENT, not the wire: between chunks a thinking agent can legitimately be
// silent for its whole timeout, and savannahd guarantees a RESULT trailer
// within it, so client budget = agent timeout + margin.
int stream_budget_ms(const Cli& cli) {
    const u32 agent_budget =
        cli.timeout_ms ? cli.timeout_ms
                       : static_cast<u32>(savannah::kDefaultAgentTimeoutMs);
    return static_cast<int>(agent_budget + kStreamMarginMs);
}

// Run one chunk stream to its end, printing as it goes. `start` is handed
// the chunk sink and the budget and makes the streaming call. Exit code 0
// iff the RESULT trailer reported a clean finish.
template <typename Start>
int run_stream(const Cli& cli, Start&& start) {
    bool ok = false;
    start([&ok](sw::AgentChunk& c) { print_chunk(c, ok); },
          stream_budget_ms(cli));
    std::cout.flush();
    return ok ? 0 : 1;
}

// The one-line outcome of a yes/no task verb, on stderr; exit code 0 or 1.
int report(bool ok, const char* yes, const char* no) {
    std::cerr << (ok ? yes : no);
    return ok ? 0 : 1;
}

void print_task(const sw::TaskInfo& t) {
    std::cout << t.id << "  " << t.state << "  turns=" << t.turns
              << "  \"" << t.title << "\"";
    if (!t.worktree.empty()) std::cout << "  " << t.worktree;
    std::cout << "\n";
    if (!t.last_line.empty()) std::cout << "    " << t.last_line << "\n";
}

// Collapse newlines/tabs so a value renders on a single table row.
std::string flatten(const std::string& s) {
    std::string out;
    for (char ch : s) out += (ch == '\n' || ch == '\r' || ch == '\t') ? ' ' : ch;
    return out;
}

// Pad/truncate to exactly w columns, plus a two-space gap.
std::string pad(std::string s, std::size_t w) {
    if (s.size() > w) s = s.substr(0, w);
    s.resize(w, ' ');
    return s + "  ";
}

// The machine-readable seam: a JSON array of tasks. Any UI (a Go TUI, a React
// dashboard) can consume this instead of speaking song's wire protocol.
std::string tasks_to_json(const sw::TaskList& list) {
    namespace j = savannah::json;
    j::Array arr;
    for (const auto& t : list.tasks) {
        j::Object o;
        o.emplace("id", j::Value(t.id));
        o.emplace("title", j::Value(t.title));
        o.emplace("state", j::Value(t.state));
        o.emplace("worktree", j::Value(t.worktree));
        o.emplace("session_id", j::Value(t.session_id));
        o.emplace("turns", j::Value(static_cast<double>(t.turns)));
        o.emplace("last_line", j::Value(t.last_line));
        arr.push_back(j::Value(std::move(o)));
    }
    return j::dump(j::Value(std::move(arr)));
}

// Leave the alternate screen and restore the cursor on the way out. Uses only
// async-signal-safe calls so it can run from the Ctrl-C handler.
void watch_restore_terminal() {
    const char* seq = "\033[?25h\033[?1049l";  // show cursor, leave alt screen
    ssize_t n = ::write(STDOUT_FILENO, seq, std::strlen(seq));
    (void)n;
}

void watch_on_signal(int) {
    watch_restore_terminal();
    _exit(0);
}

// The cheap live view: poll task_list once a second and redraw a table in
// place, top-style. Uses the terminal's alternate screen buffer (like
// top/htop/vim) so it redraws over itself instead of scrolling, and restores
// the original screen on exit. No server changes, no push; a UI-flow starter
// the push-based dashboard can replace underneath the same rendering later.
int run_task_watch(sw::AgentNodeProxy& proxy, const std::string& node) {
    std::signal(SIGINT, watch_on_signal);
    std::signal(SIGTERM, watch_on_signal);
    std::cout << "\033[?1049h\033[?25l";  // enter alt screen, hide cursor
    std::cout.flush();

    for (;;) {
        sw::TaskList list = proxy.task_list();
        std::time_t now = std::time(nullptr);
        char ts[16] = "";
        std::strftime(ts, sizeof ts, "%H:%M:%S", std::localtime(&now));

        // Build one frame, homing the cursor and clearing each line to the end
        // (so a shorter line does not leave stale characters), then clearing to
        // the end of screen (so a shrunk table leaves no orphan rows).
        std::string f = "\033[H";
        auto line = [&](const std::string& s) { f += s + "\033[K\n"; };
        line("watching " + node + "   " + std::to_string(list.tasks.size()) +
             " task" + (list.tasks.size() == 1 ? "" : "s") + "   " + ts +
             "   [Ctrl-C to quit]");
        line("");
        if (list.tasks.empty()) {
            line("  (no tasks yet)");
        } else {
            line(pad("ID", 7) + pad("STATE", 11) + pad("TURN", 4) +
                 pad("TITLE", 22) + "LAST LINE");
            for (const auto& t : list.tasks) {
                line(pad(t.id, 7) + pad(t.state, 11) +
                     pad(std::to_string(t.turns), 4) + pad(flatten(t.title), 22) +
                     flatten(t.last_line).substr(0, 56));
            }
        }
        f += "\033[J";  // clear from cursor to end of screen
        std::cout << f;
        std::cout.flush();
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    return 0;  // unreachable: a signal restores the terminal and exits
}

int task_new(sw::AgentNodeProxy& proxy, const Cli& cli) {
    if (cli.task_title.empty()) {
        std::cerr << "savannah task new: --title is required\n";
        return 2;
    }
    sw::TaskSpec spec;
    spec.title = cli.task_title;
    spec.prompt = cli.prompt;  // --prompt; empty = create idle, drive later
    spec.workdir = cli.task_workdir;
    spec.make_worktree = cli.task_worktree;
    spec.timeout_ms = cli.timeout_ms;
    print_task(proxy.task_new(spec));
    return 0;
}

int task_ls(sw::AgentNodeProxy& proxy, const Cli& cli) {
    if (cli.task_watch) return run_task_watch(proxy, cli.node);
    sw::TaskList list = proxy.task_list();
    if (cli.json_out) {
        std::cout << tasks_to_json(list) << "\n";
        return 0;
    }
    if (list.tasks.empty()) {
        std::cerr << "(no tasks)\n";
        return 0;
    }
    for (const auto& t : list.tasks) print_task(t);
    return 0;
}

int task_status(sw::AgentNodeProxy& proxy, const Cli& cli) {
    print_task(proxy.task_status(cli.task_id));
    return 0;
}

int task_send(sw::AgentNodeProxy& proxy, const Cli& cli) {
    return report(proxy.task_send(cli.task_id, cli.prompt), "sent\n",
                  "not sent (task busy or gone)\n");
}

// Replay the transcript, then follow the live turn to its RESULT.
int task_tail(sw::AgentNodeProxy& proxy, const Cli& cli) {
    return run_stream(cli, [&](const auto& sink, int budget_ms) {
        proxy.task_output(cli.task_id, sink, budget_ms);
    });
}

int task_cancel(sw::AgentNodeProxy& proxy, const Cli& cli) {
    return report(proxy.task_cancel(cli.task_id), "cancelled\n",
                  "no such task\n");
}

int task_rm(sw::AgentNodeProxy& proxy, const Cli& cli) {
    return report(proxy.task_rm(cli.task_id), "removed\n",
                  "no such task (a running task needs cancel first)\n");
}

// Every task sub-verb: what is valid, what positionals it takes, what runs.
// (The usage text above spells the same set out for people.)
constexpr TaskVerb kTaskVerbs[] = {
    //  name      id     prompt  run
    {"new",    false, false, task_new},
    {"ls",     false, false, task_ls},
    {"status", true,  false, task_status},
    {"send",   true,  true,  task_send},
    {"tail",   true,  false, task_tail},
    {"cancel", true,  false, task_cancel},
    {"rm",     true,  false, task_rm},
};

int node_info(sw::AgentNodeProxy& proxy, const Cli&) {
    auto info = proxy.info();
    std::cout << "name:      " << info.name << "\n"
              << "model:     " << info.model << "\n"
              << "workspace: " << info.workspace << "\n"
              << "tags:      ";
    for (std::size_t i = 0; i < info.tags.size(); ++i) {
        std::cout << (i ? "," : "") << info.tags[i];
    }
    std::cout << "\nprotocol:  v" << info.protocol_ver << "\n";
    return 0;
}

int node_status(sw::AgentNodeProxy& proxy, const Cli&) {
    std::cout << proxy.status() << "\n";
    return 0;
}

// ask, streamed live: chunks print as they arrive (song's incremental
// call_streaming). The generated streaming proxy (song finding 6) encodes
// the args, calls over kService_AgentNode_Stream, and hands each chunk
// back already decoded.
int node_ask(sw::AgentNodeProxy& proxy, const Cli& cli) {
    sw::AskOptions opts;
    opts.timeout_ms = cli.timeout_ms;
    opts.fresh_context = true;
    opts.system_hint = "";
    return run_stream(cli, [&](const auto& sink, int budget_ms) {
        proxy.ask(cli.prompt, opts, sink, budget_ms);
    });
}

// Every verb that talks to one node, besides `task`.
constexpr NodeVerb kNodeVerbs[] = {
    //  name      prompt  run
    {"ask",    true,  node_ask},
    {"info",   false, node_info},
    {"status", false, node_status},
};

// argv -> Cli. False is a usage error: too few positionals for the verb, a
// bad flag, or a verb (or task sub-verb) that does not exist.
//   savannah ls [flags]
//   savannah <ask|info|status> <node> [prompt] [flags]
//   savannah task <sub> <node> [id] [prompt] [flags]
bool parse_cli(int argc, char** argv, Cli& c) {
    if (argc < 2) return false;
    c.cmd = argv[1];
    int i = 2;
    // The next positional, or false when argv has run out.
    auto take = [&](std::string& into) {
        if (i >= argc) return false;
        into = argv[i++];
        return true;
    };
    bool needs_id = false;
    bool needs_prompt = false;
    if (c.cmd == "task") {
        std::string sub;
        if (argc < 4 || !take(sub) || !take(c.node)) return false;
        c.task = find_verb(kTaskVerbs, sub);
        if (c.task) {
            needs_id = c.task->needs_id;
            needs_prompt = c.task->needs_prompt;
        }
    } else if (c.cmd != "ls") {
        if (!take(c.node)) return false;
        c.verb = find_verb(kNodeVerbs, c.cmd);
        // An unknown verb is refused below, after its flags are looked at;
        // like the known ones, it never reads a missing prompt.
        needs_prompt = c.verb && c.verb->needs_prompt;
    }
    if (needs_id && !take(c.task_id)) return false;
    if (needs_prompt && !take(c.prompt)) return false;
    if (!parse_flags(argc, argv, i, c)) return false;
    return c.cmd == "ls" || c.task != nullptr || c.verb != nullptr;
}

// Run the parsed verb against a connected node.
int run_command(ServiceConnection& conn, const Cli& cli) {
    sw::AgentNodeProxy proxy(conn);
    return cli.task ? cli.task->run(proxy, cli) : cli.verb->run(proxy, cli);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 1) return exec_dash(argv[0], {});
    if (std::strncmp(argv[1], "--", 2) == 0 && std::strcmp(argv[1], "--help") != 0) {
        // Flags before any verb are the board's (`savannah --group`).
        return exec_dash(argv[0], tail(argc, argv, 1));
    }
    if (std::strcmp(argv[1], "mesh") == 0) {
        std::vector<std::string> rest = tail(argc, argv, 2);
        rest.insert(rest.begin(), "--mesh");
        return exec_dash(argv[0], rest);
    }
    if (std::strcmp(argv[1], "hub") == 0 || std::strcmp(argv[1], "work") == 0) {
        std::vector<std::string> rest = tail(argc, argv, 2);
        if (std::strcmp(argv[1], "hub") == 0) rest.insert(rest.begin(), "hub");
        return exec_work(rest);
    }

    Cli cli;
    if (!parse_cli(argc, argv, cli)) return usage();

    try {
        if (cli.cmd == "ls") return run_ls(cli);

        if (cli.node == "local") {
            if (!cli.config.empty()) {
                setenv("SAVANNAHD_CONFIG", cli.config.c_str(), 1);
            }
            song::ServiceManager mgr;
            mgr.register_service("agent", "./savannahd", 1);
            ServiceConnection conn = mgr.connect("agent");
            return run_command(conn, cli);
        }

        ServiceConnection conn = connect_remote(cli);
        return run_command(conn, cli);
    } catch (const std::exception& e) {
        std::cerr << "savannah: " << e.what() << "\n";
        return 1;
    }
}
