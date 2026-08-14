// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// savannah — the human's remote control (and the test harness).
//
//   savannah ls [--timeout-ms N]
//   savannah ask <node> "<prompt>" [options]
//   savannah info <node> [options]
//   savannah status <node> [options]
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

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iostream>
#include <memory>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "savannah.hpp"
#include "../src/config.hpp"    // load_hmac_key
#include "../src/json.hpp"      // --json output
#include "../src/wire_ids.hpp"  // stream service id + mesh mDNS type

namespace sw = song::savannah;

using namespace song;

namespace {

int usage() {
    std::cerr <<
        "usage: savannah ls [--timeout-ms N]\n"
        "       savannah <ask|info|status> <node> [\"prompt\"]\n"
        "       savannah task new    <node> --title T [--prompt P]\n"
        "                                   [--worktree] [--workdir DIR]\n"
        "       savannah task ls     <node> [--watch] [--json]\n"
        "       savannah task status <node> <id>\n"
        "       savannah task send   <node> <id> \"prompt\"\n"
        "       savannah task tail   <node> <id>\n"
        "       savannah task cancel <node> <id>\n"
        "               [--config PATH] [--timeout-ms N]\n"
        "               [--addr HOST:PORT] [--key FILE]\n"
        "\"local\" spawns ./savannahd over pipes; any other node is resolved\n"
        "via mDNS (or --addr) and reached over TCP, HMAC'd when --key is set.\n";
    return 2;
}

struct Cli {
    std::string cmd;
    std::string node;
    std::string prompt;
    std::string config;
    std::string addr;
    std::string key_file;
    u32 timeout_ms = 0;

    // `task` subcommands: `savannah task <sub> <node> [id] [prompt] [flags]`
    std::string task_sub;      // new | ls | status | send | tail | cancel
    std::string task_id;
    std::string task_title;    // --title (task new)
    std::string task_workdir;  // --workdir (task new)
    bool task_worktree = false;  // --worktree (task new)
    bool task_watch = false;     // --watch (task ls): poll + redraw
    bool json_out = false;       // --json (task ls): machine-readable output
};

bool task_sub_valid(const std::string& s) {
    return s == "new" || s == "ls" || s == "status" || s == "send" ||
           s == "tail" || s == "cancel";
}

bool parse_cli(int argc, char** argv, Cli& c) {
    if (argc < 2) return false;
    c.cmd = argv[1];
    int i;
    if (c.cmd == "task") {
        // task <sub> <node> [id] [prompt]
        if (argc < 4) return false;
        c.task_sub = argv[2];
        c.node = argv[3];
        i = 4;
        // subcommands that operate on an existing task take a positional id
        // (and `send` also a positional prompt).
        bool needs_id = (c.task_sub == "status" || c.task_sub == "send" ||
                         c.task_sub == "tail" || c.task_sub == "cancel");
        if (needs_id) {
            if (i >= argc) return false;
            c.task_id = argv[i++];
            if (c.task_sub == "send") {
                if (i >= argc) return false;
                c.prompt = argv[i++];
            }
        }
    } else {
        i = 2;
        if (c.cmd != "ls") {
            if (argc < 3) return false;
            c.node = argv[2];
            i = 3;
            if (c.cmd == "ask") {
                if (argc < 4) return false;
                c.prompt = argv[3];
                i = 4;
            }
        }
    }
    for (; i < argc; ++i) {
        if (std::strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            c.config = argv[++i];
        } else if (std::strcmp(argv[i], "--timeout-ms") == 0 && i + 1 < argc) {
            c.timeout_ms = static_cast<u32>(std::strtoul(argv[++i], nullptr, 10));
        } else if (std::strcmp(argv[i], "--addr") == 0 && i + 1 < argc) {
            c.addr = argv[++i];
        } else if (std::strcmp(argv[i], "--key") == 0 && i + 1 < argc) {
            c.key_file = argv[++i];
        } else if (std::strcmp(argv[i], "--title") == 0 && i + 1 < argc) {
            c.task_title = argv[++i];
        } else if (std::strcmp(argv[i], "--prompt") == 0 && i + 1 < argc) {
            c.prompt = argv[++i];
        } else if (std::strcmp(argv[i], "--workdir") == 0 && i + 1 < argc) {
            c.task_workdir = argv[++i];
        } else if (std::strcmp(argv[i], "--worktree") == 0) {
            c.task_worktree = true;
        } else if (std::strcmp(argv[i], "--watch") == 0) {
            c.task_watch = true;
        } else if (std::strcmp(argv[i], "--json") == 0) {
            c.json_out = true;
        } else {
            return false;
        }
    }
    if (c.cmd == "task") return task_sub_valid(c.task_sub);
    return c.cmd == "ls" || c.cmd == "ask" || c.cmd == "info" ||
           c.cmd == "status";
}

int run_ls(const Cli& cli) {
    auto discovery = create_discovery();
    if (!discovery || !discovery->is_available()) {
        std::cerr << "savannah: mDNS not available on this machine\n";
        return 1;
    }
    auto timeout =
        std::chrono::milliseconds(cli.timeout_ms ? cli.timeout_ms : 2000);
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
        port = static_cast<u16>(
            std::strtoul(cli.addr.c_str() + colon + 1, nullptr, 10));
    } else {
        auto discovery = create_discovery();
        if (!discovery || !discovery->is_available()) {
            throw std::runtime_error(
                "mDNS not available; use --addr HOST:PORT");
        }
        auto s = discovery->discover_one(
            cli.node, savannah_wire::kMeshServiceType,
            std::chrono::milliseconds(3000));
        if (!s) {
            throw std::runtime_error("node \"" + cli.node +
                                     "\" not found on the mesh");
        }
        host = s->host;
        port = s->port;
    }

    auto tcp = std::make_unique<TcpTransport>();
    tcp->connect(host, port, 5000);
    std::unique_ptr<Transport> transport = std::move(tcp);
    if (!cli.key_file.empty()) {
        transport = std::make_unique<SecureTransport>(
            std::move(transport),
            SecurityConfig(::savannah::load_hmac_key(cli.key_file)));
    }
    ServiceConnection conn(std::move(transport));
    conn.init_handshake();
    return conn;
}

// Shared chunk sink for ask and task tail (same stream contract): TEXT ->
// stdout verbatim, TOOL_EVENT/RESULT -> stderr. Sets ok from the RESULT trailer.
void print_chunk(const sw::AgentChunk& c, bool& ok) {
    switch (c.kind) {
        case 0:  // TEXT
            std::cout << c.payload;
            std::cout.flush();
            break;
        case 1:  // TOOL_EVENT
            std::cerr << "[tool] " << c.payload << "\n";
            break;
        case 2:  // RESULT
            std::cerr << "\n[result] " << c.payload << "\n";
            ok = c.payload.find("\"is_error\":false") != std::string::npos;
            break;
        default:
            std::cerr << "[?] unknown chunk kind " << c.kind << "\n";
            break;
    }
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
    namespace j = ::savannah::json;
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

// The cheap live view: poll task_list once a second and redraw a table. No
// server changes, no push; a UI-flow starter that the push-based dashboard can
// replace underneath the same idea later. Ctrl-C exits.
int run_task_watch(sw::AgentNodeProxy& proxy, const std::string& node) {
    for (;;) {
        sw::TaskList list = proxy.task_list();
        std::time_t now = std::time(nullptr);
        char ts[16] = "";
        std::strftime(ts, sizeof ts, "%H:%M:%S", std::localtime(&now));
        std::cout << "\033[2J\033[H"  // clear screen, home cursor
                  << "watching " << node << "   " << list.tasks.size()
                  << " task" << (list.tasks.size() == 1 ? "" : "s") << "   "
                  << ts << "   [Ctrl-C to quit]\n\n";
        if (list.tasks.empty()) {
            std::cout << "  (no tasks yet)\n";
        } else {
            std::cout << pad("ID", 7) << pad("STATE", 10) << pad("TURN", 4)
                      << pad("TITLE", 22) << "LAST LINE\n";
            for (const auto& t : list.tasks) {
                std::cout << pad(t.id, 7) << pad(t.state, 10)
                          << pad(std::to_string(t.turns), 4)
                          << pad(flatten(t.title), 22)
                          << flatten(t.last_line).substr(0, 56) << "\n";
            }
        }
        std::cout.flush();
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    return 0;  // unreachable: Ctrl-C terminates the process
}

int run_task(ServiceConnection& conn, const Cli& cli) {
    sw::AgentNodeProxy proxy(conn);
    const std::string& sub = cli.task_sub;

    if (sub == "new") {
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
    if (sub == "ls") {
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
    if (sub == "status") {
        print_task(proxy.task_status(cli.task_id));
        return 0;
    }
    if (sub == "send") {
        bool ok = proxy.task_send(cli.task_id, cli.prompt);
        std::cerr << (ok ? "sent\n" : "not sent (task busy or gone)\n");
        return ok ? 0 : 1;
    }
    if (sub == "cancel") {
        bool ok = proxy.task_cancel(cli.task_id);
        std::cerr << (ok ? "cancelled\n" : "no such task\n");
        return ok ? 0 : 1;
    }
    // tail: replay the transcript, then follow the live turn to its RESULT.
    constexpr u32 kDefaultAgentTimeoutMs = 300000;
    constexpr u32 kMarginMs = 30000;
    u32 budget = cli.timeout_ms ? cli.timeout_ms : kDefaultAgentTimeoutMs;
    bool ok = false;
    proxy.task_output(
        cli.task_id,
        [&ok](sw::AgentChunk& c) { print_chunk(c, ok); },
        static_cast<int>(budget + kMarginMs));
    std::cout.flush();
    return ok ? 0 : 1;
}

int run_command(ServiceConnection& conn, const Cli& cli) {
    if (cli.cmd == "task") return run_task(conn, cli);

    if (cli.cmd == "info") {
        Buffer req;
        Buffer resp = conn.call(sw::kService_AgentNode,
                                 sw::kMethod_AgentNode_info, req);
        auto info = sw::decode_AgentInfo(resp);
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

    if (cli.cmd == "status") {
        Buffer req;
        Buffer resp = conn.call(sw::kService_AgentNode,
                                 sw::kMethod_AgentNode_status, req);
        std::cout << decode_string(resp) << "\n";
        return 0;
    }

    // ask, streamed live: chunks print as they arrive (song's incremental
    // call_streaming). The chunk timeout is sized to the AGENT, not the
    // wire: between chunks a thinking agent can legitimately be silent for
    // its whole timeout budget, and savannahd guarantees a RESULT trailer
    // within it, so client budget = agent timeout + margin.
    sw::AskOptions opts;
    opts.timeout_ms = cli.timeout_ms;
    opts.fresh_context = true;
    opts.system_hint = "";

    constexpr u32 kDefaultAgentTimeoutMs = 300000;  // savannahd's default
    constexpr u32 kMarginMs = 30000;
    u32 agent_budget =
        cli.timeout_ms ? cli.timeout_ms : kDefaultAgentTimeoutMs;

    // The generated streaming proxy (song finding 6) encodes the args, calls
    // over kService_AgentNode_Stream, and hands each chunk back already decoded.
    bool ok = false;
    sw::AgentNodeProxy proxy(conn);
    proxy.ask(
        cli.prompt, opts,
        [&ok](sw::AgentChunk& c) { print_chunk(c, ok); },
        static_cast<int>(agent_budget + kMarginMs));
    std::cout.flush();
    return ok ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    Cli cli;
    if (!parse_cli(argc, argv, cli)) return usage();

    try {
        if (cli.cmd == "ls") return run_ls(cli);

        if (cli.node == "local") {
            if (!cli.config.empty()) {
                setenv("SAVANNAHD_CONFIG", cli.config.c_str(), 1);
            }
            ServiceManager mgr;
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
