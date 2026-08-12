// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Integration: the task supervisor end to end, token-free. Spawns
// savannahd --tcp 0 against a session-aware fake-claude and a scratch git
// repo, then drives the full lifecycle over TCP:
//   task_new (with worktree) -> turn 1 runs -> tail it
//   task_send -> turn 2 resumes the SAME session (counter carries)
//   a second task is isolated (own worktree, own session id)
//   cancel one -> the other still drives
//
// Needs cwd = build dir (ctest sets WORKING_DIRECTORY) with ./savannahd,
// ./fake-claude, cfg_task.toml. Skips (77) if git is unavailable.

#include <song/song.hpp>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>

#include "savannah.hpp"
#include "../src/wire_ids.hpp"

namespace sw = song::savannah;
using song::Buffer;
using song::ServiceConnection;
using song::u16;

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

namespace {

struct Daemon {
    pid_t pid = -1;
    u16 port = 0;

    static Daemon spawn(const char* config) {
        Daemon d;
        int fds[2];
        if (pipe(fds) != 0) return d;
        pid_t pid = fork();
        if (pid < 0) {
            close(fds[0]);
            close(fds[1]);
            return d;
        }
        if (pid == 0) {
            dup2(fds[1], STDOUT_FILENO);
            close(fds[0]);
            close(fds[1]);
            setenv("SAVANNAHD_CONFIG", config, 1);
            execl("./savannahd", "savannahd", "--tcp", "0",
                  static_cast<char*>(nullptr));
            _exit(127);
        }
        close(fds[1]);
        std::string line;
        char c;
        while (read(fds[0], &c, 1) == 1 && c != '\n') line.push_back(c);
        close(fds[0]);
        const char* marker = "SAVANNAHD_TCP_PORT=";
        if (line.rfind(marker, 0) == 0) {
            d.pid = pid;
            d.port = static_cast<u16>(
                std::strtoul(line.c_str() + std::strlen(marker), nullptr, 10));
        } else {
            kill(pid, SIGKILL);
            waitpid(pid, nullptr, 0);
        }
        return d;
    }

    ~Daemon() {
        if (pid > 0) {
            kill(pid, SIGKILL);
            waitpid(pid, nullptr, 0);
        }
    }
};

ServiceConnection connect(u16 port) {
    auto tcp = std::make_unique<song::TcpTransport>();
    tcp->connect("127.0.0.1", port, 5000);
    ServiceConnection conn(std::move(tcp));
    conn.init_handshake();
    return conn;
}

sw::TaskInfo task_new(ServiceConnection& conn, const std::string& title,
                      const std::string& prompt, bool worktree) {
    sw::TaskSpec spec;
    spec.title = title;
    spec.prompt = prompt;
    spec.workdir = "";
    spec.make_worktree = worktree;
    spec.timeout_ms = 0;
    Buffer args;
    sw::encode_TaskSpec(args, spec);
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_task_new, args);
    return sw::decode_TaskInfo(resp);
}

sw::TaskInfo task_status(ServiceConnection& conn, const std::string& id) {
    Buffer args;
    song::encode_string(args, id);
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_task_status, args);
    return sw::decode_TaskInfo(resp);
}

bool task_send(ServiceConnection& conn, const std::string& id,
               const std::string& prompt) {
    Buffer args;
    song::encode_string(args, id);
    song::encode_string(args, prompt);
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_task_send, args);
    return song::decode_bool(resp);
}

bool task_cancel(ServiceConnection& conn, const std::string& id) {
    Buffer args;
    song::encode_string(args, id);
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_task_cancel, args);
    return song::decode_bool(resp);
}

std::size_t task_count(ServiceConnection& conn) {
    Buffer args;
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_task_list, args);
    return sw::decode_TaskList(resp).tasks.size();
}

// Tail a task; concatenate its TEXT chunks.
std::string task_output(ServiceConnection& conn, const std::string& id) {
    Buffer args;
    song::encode_string(args, id);
    std::string text;
    conn.call_streaming(savannah_wire::kService_AgentNode_Stream,
                        sw::kMethod_AgentNode_task_output, args,
                        [&](Buffer& chunk) {
                            auto c = sw::decode_AgentChunk(chunk);
                            if (c.kind == 0) text += c.payload;
                        },
                        /*chunk_timeout_ms=*/30000);
    return text;
}

// Poll task status until state == want (or budget elapses).
sw::TaskInfo wait_state(ServiceConnection& conn, const std::string& id,
                        const std::string& want, int budget_ms = 10000) {
    sw::TaskInfo info;
    for (int waited = 0; waited < budget_ms; waited += 20) {
        info = task_status(conn, id);
        if (info.state == want) return info;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return info;
}

}  // namespace

int main() {
    if (std::system("git --version >/dev/null 2>&1") != 0) {
        std::printf("test_task_flight: skipped (git not available)\n");
        return 77;
    }

    // fake-claude's session counter lives under $TMPDIR; point it at this
    // build dir (guaranteed writable) so the count survives across turns
    // regardless of the worker's worktree cwd.
    char cwd[4096];
    if (getcwd(cwd, sizeof cwd)) setenv("TMPDIR", cwd, 1);
    std::system("rm -f fake-claude-session-*.count");

    // Scratch git repo the worktrees branch from.
    std::system(
        "rm -rf task_repo && git init -q task_repo && "
        "git -C task_repo config user.email t@t && "
        "git -C task_repo config user.name t && "
        "git -C task_repo commit -q --allow-empty -m init");

    Daemon d = Daemon::spawn("cfg_task.toml");
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (g_failures) return 1;

    auto conn = connect(d.port);

    // ---- task_new with a worktree drives turn 1 ----
    auto t1 = task_new(conn, "adder", "add a feature", /*worktree=*/true);
    CHECK(!t1.id.empty());
    CHECK(!t1.session_id.empty());
    CHECK(t1.worktree.find(".savannah-worktrees") != std::string::npos);

    auto s1 = wait_state(conn, t1.id, "idle");
    CHECK(s1.state == "idle");
    CHECK(s1.turns == 1);
    CHECK(s1.last_line.find("turn 1") != std::string::npos);

    // ---- tail replays the transcript ----
    CHECK(task_output(conn, t1.id).find("turn 1") != std::string::npos);

    // ---- task_send resumes the SAME session: counter carries to turn 2 ----
    CHECK(task_send(conn, t1.id, "keep going") == true);
    auto s2 = wait_state(conn, t1.id, "idle");
    CHECK(s2.turns == 2);
    CHECK(s2.session_id == t1.session_id);  // stable session id
    CHECK(s2.last_line.find("turn 2") != std::string::npos);  // context carried

    // ---- a second task is isolated ----
    auto t2 = task_new(conn, "docs", "update the readme", /*worktree=*/true);
    wait_state(conn, t2.id, "idle");
    CHECK(t2.id != t1.id);
    CHECK(t2.session_id != t1.session_id);
    CHECK(t2.worktree != t1.worktree);
    CHECK(task_count(conn) == 2);

    // ---- cancel t1; t2 still drives ----
    CHECK(task_cancel(conn, t1.id) == true);
    CHECK(task_status(conn, t1.id).state == "cancelled");
    CHECK(task_send(conn, t2.id, "one more") == true);
    auto s2b = wait_state(conn, t2.id, "idle");
    CHECK(s2b.turns == 2);

    // ---- a cancelled task rejects further sends; unknown ids too ----
    CHECK(task_send(conn, t1.id, "nope") == false);
    CHECK(task_send(conn, "t-9999", "nope") == false);

    if (g_failures == 0) std::printf("test_task_flight: all passed\n");
    return g_failures == 0 ? 0 : 1;
}
