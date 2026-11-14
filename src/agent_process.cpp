// MIT License
// Copyright (c) 2026 Dennis B Jones

#include "agent_process.hpp"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cerrno>
#include <cstring>

#include "config.hpp"

namespace savannah {

namespace {

using Clock = std::chrono::steady_clock;

// The read loop wakes at least this often so cancel and timeout stay
// responsive while the agent is silent.
constexpr std::int64_t kPollTickMs = 100;

// One read() from the agent's stdout pipe.
constexpr std::size_t kReadBufferBytes = 4096;

std::int64_t ms_since(Clock::time_point start) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               Clock::now() - start)
        .count();
}

}  // namespace

const char* failure_reason(const AgentOutcome& outcome) {
    if (!outcome.spawned) return "spawn failed";
    if (outcome.timed_out) return "timeout";
    if (outcome.killed) return "cancelled";
    return "died before result";
}

std::vector<std::string> base_agent_argv(const NodeConfig& config,
                                         const std::string& prompt) {
    std::vector<std::string> argv;
    argv.push_back(config.agent_cmd);
    for (const auto& a : config.agent_args) argv.push_back(a);
    argv.push_back("-p");
    argv.push_back(prompt);
    argv.push_back("--output-format");
    argv.push_back("stream-json");
    argv.push_back("--verbose");
    argv.push_back("--max-turns");
    argv.push_back(std::to_string(config.max_turns));
    if (!config.allowed_tools.empty()) {
        argv.push_back("--allowedTools");
        std::string joined;
        for (const auto& t : config.allowed_tools) {
            if (!joined.empty()) joined += ",";
            joined += t;
        }
        argv.push_back(joined);
    }
    return argv;
}

AgentOutcome AgentProcess::run(const std::vector<std::string>& argv,
                               std::int64_t timeout_ms,
                               const std::atomic<bool>& cancel_flag,
                               ChunkMapper::Sink sink,
                               const std::string& cwd) {
    AgentOutcome out;
    if (argv.empty()) return out;

    // Close-on-exec on both ends: a child another thread forks (a second
    // task worker) must not inherit this agent's write end, or the read loop
    // below would wait for that child too. dup2 in our own child clears the
    // flag on its stdout copy.
    int fds[2];
#if defined(__linux__)
    if (pipe2(fds, O_CLOEXEC) != 0) return out;
#else
    if (pipe(fds) != 0) return out;
    fcntl(fds[0], F_SETFD, FD_CLOEXEC);
    fcntl(fds[1], F_SETFD, FD_CLOEXEC);
#endif

    pid_t pid = fork();
    if (pid < 0) {
        close(fds[0]);
        close(fds[1]);
        return out;
    }

    if (pid == 0) {
        // Child: stdout -> pipe, stdin -> /dev/null, stderr inherited.
        dup2(fds[1], STDOUT_FILENO);
        close(fds[0]);
        close(fds[1]);
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            dup2(devnull, STDIN_FILENO);
            close(devnull);
        }
        if (!cwd.empty() && chdir(cwd.c_str()) != 0) {
            _exit(126);  // worktree vanished; surfaces as spawn-ish failure
        }
        std::vector<char*> cargv = to_cargv(argv);
        execvp(cargv[0], cargv.data());
        _exit(127);
    }

    // Parent.
    close(fds[1]);
    out.spawned = true;

    ChunkMapper mapper(std::move(sink));
    std::string carry;
    char buf[kReadBufferBytes];
    const auto start = Clock::now();
    bool child_running = true;

    auto kill_child = [&] {
        if (child_running) kill(pid, SIGKILL);
    };

    // The sink can throw (a stream write to a vanished client, an encode
    // refusal). The child must still be killed and reaped on that path or
    // it lingers forever blocked on a full pipe.
    try {
        for (;;) {
            if (cancel_flag.load(std::memory_order_relaxed)) {
                out.killed = true;
                kill_child();
                break;
            }
            std::int64_t elapsed = ms_since(start);
            if (elapsed >= timeout_ms) {
                out.timed_out = true;
                kill_child();
                break;
            }

            struct pollfd pfd{};
            pfd.fd = fds[0];
            pfd.events = POLLIN;
            std::int64_t remaining = timeout_ms - elapsed;
            int wait_ms = static_cast<int>(
                remaining < kPollTickMs ? remaining : kPollTickMs);
            int rc = poll(&pfd, 1, wait_ms);
            if (rc < 0) {
                if (errno == EINTR) continue;
                kill_child();
                break;
            }
            if (rc == 0) continue;  // poll tick: re-check cancel/timeout

            ssize_t n = read(fds[0], buf, sizeof buf);
            if (n < 0) {
                if (errno == EINTR) continue;
                kill_child();
                break;
            }
            if (n == 0) break;  // EOF: child closed stdout

            carry.append(buf, static_cast<std::size_t>(n));
            std::size_t nl;
            while ((nl = carry.find('\n')) != std::string::npos) {
                std::string line = carry.substr(0, nl);
                carry.erase(0, nl + 1);
                mapper.feed_line(line);
            }
        }

        // Trailing partial line (agent died mid-write): still worth feeding.
        if (!carry.empty()) mapper.feed_line(carry);
    } catch (...) {
        kill(pid, SIGKILL);
        close(fds[0]);
        int status = 0;
        waitpid(pid, &status, 0);
        throw;
    }

    close(fds[0]);

    int status = 0;
    if (waitpid(pid, &status, 0) == pid) {
        child_running = false;
        if (WIFEXITED(status)) out.exit_code = WEXITSTATUS(status);
        else if (WIFSIGNALED(status)) out.exit_code = 128 + WTERMSIG(status);
    }

    out.saw_result = mapper.saw_result();
    out.max_turns_reached = mapper.max_turns_reached();
    out.malformed_lines = mapper.malformed_lines();
    return out;
}

}  // namespace savannah
