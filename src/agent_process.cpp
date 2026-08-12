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

namespace savannah {

namespace {

using Clock = std::chrono::steady_clock;

std::int64_t ms_since(Clock::time_point start) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               Clock::now() - start)
        .count();
}

}  // namespace

AgentOutcome AgentProcess::run(const std::vector<std::string>& argv,
                               std::int64_t timeout_ms,
                               const std::atomic<bool>& cancel_flag,
                               ChunkMapper::Sink sink,
                               const std::string& cwd) {
    AgentOutcome out;
    if (argv.empty()) return out;

    int fds[2];
    if (pipe(fds) != 0) return out;

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
        std::vector<char*> cargv;
        cargv.reserve(argv.size() + 1);
        for (const auto& a : argv) {
            cargv.push_back(const_cast<char*>(a.c_str()));
        }
        cargv.push_back(nullptr);
        execvp(cargv[0], cargv.data());
        _exit(127);
    }

    // Parent.
    close(fds[1]);
    out.spawned = true;

    ChunkMapper mapper(std::move(sink));
    std::string carry;
    char buf[4096];
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
            // Wake at least every 100ms so cancel/timeout stay responsive.
            std::int64_t remaining = timeout_ms - elapsed;
            int wait_ms = static_cast<int>(remaining < 100 ? remaining : 100);
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
    out.malformed_lines = mapper.malformed_lines();
    return out;
}

}  // namespace savannah
