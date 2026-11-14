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

void UniqueFd::reset(int fd) {
    if (fd_ >= 0) close(fd_);
    fd_ = fd;
}

Child::~Child() {
    if (pid_ <= 0) return;
    kill();
    wait();
}

void Child::kill() {
    if (pid_ > 0) ::kill(pid_, SIGKILL);
}

std::optional<int> Child::wait() {
    if (pid_ <= 0) return std::nullopt;
    const pid_t pid = pid_;
    pid_ = -1;
    int status = 0;
    if (waitpid(pid, &status, 0) != pid) return std::nullopt;
    return status;
}

int exit_code(int wait_status) {
    if (WIFEXITED(wait_status)) return WEXITSTATUS(wait_status);
    if (WIFSIGNALED(wait_status)) return 128 + WTERMSIG(wait_status);
    return -1;
}

Child spawn(const std::vector<std::string>& argv, int stdout_fd,
            const std::string& cwd) {
    if (argv.empty()) return Child();
    // Built before the fork: the child only makes system calls.
    std::vector<char*> cargv = to_cargv(argv);

    pid_t pid = fork();
    if (pid != 0) return Child(pid);  // parent, or a failed fork (-1)

    // Child. A /dev/null that cannot be opened leaves the stream inherited.
    int in = open("/dev/null", O_RDONLY);
    if (in >= 0) {
        dup2(in, STDIN_FILENO);
        if (in != STDIN_FILENO) close(in);
    }
    if (stdout_fd >= 0) {
        dup2(stdout_fd, STDOUT_FILENO);
    } else {
        int out = open("/dev/null", O_WRONLY);
        if (out >= 0) {
            dup2(out, STDOUT_FILENO);
            dup2(out, STDERR_FILENO);
            if (out > STDERR_FILENO) close(out);
        }
    }
    if (!cwd.empty() && chdir(cwd.c_str()) != 0) {
        _exit(126);  // worktree vanished; surfaces as spawn-ish failure
    }
    execvp(cargv[0], cargv.data());
    _exit(127);
}

namespace {

// The agent's stdout pipe. Close-on-exec on both ends: a child another
// thread forks (a second task worker) must not inherit this agent's write
// end, or the read loop would wait for that child too. dup2 in our own
// child clears the flag on its stdout copy.
bool cloexec_pipe(UniqueFd& read_end, UniqueFd& write_end) {
    int fds[2];
#if defined(__linux__)
    if (pipe2(fds, O_CLOEXEC) != 0) return false;
#else
    if (pipe(fds) != 0) return false;
    fcntl(fds[0], F_SETFD, FD_CLOEXEC);
    fcntl(fds[1], F_SETFD, FD_CLOEXEC);
#endif
    read_end.reset(fds[0]);
    write_end.reset(fds[1]);
    return true;
}

// Hand every complete line in `carry` to the mapper; a trailing partial
// line stays behind for the next read.
void feed_lines(std::string& carry, ChunkMapper& mapper) {
    std::size_t nl;
    while ((nl = carry.find('\n')) != std::string::npos) {
        std::string line = carry.substr(0, nl);
        carry.erase(0, nl + 1);
        mapper.feed_line(line);
    }
}

// Read the agent's stdout into the mapper until EOF, cancel, timeout or a
// read error; everything but EOF kills the child (the caller reaps it).
// Wakes every kPollTickMs so cancel and timeout are seen while the agent is
// silent. Throws whatever the sink throws.
void pump(int fd, std::int64_t timeout_ms, const std::atomic<bool>& cancel_flag,
          ChunkMapper& mapper, Child& child, AgentOutcome& out) {
    std::string carry;
    char buf[kReadBufferBytes];
    const auto start = Clock::now();

    for (;;) {
        if (cancel_flag.load(std::memory_order_relaxed)) {
            out.killed = true;
            child.kill();
            break;
        }
        std::int64_t elapsed = ms_since(start);
        if (elapsed >= timeout_ms) {
            out.timed_out = true;
            child.kill();
            break;
        }

        struct pollfd pfd{};
        pfd.fd = fd;
        pfd.events = POLLIN;
        std::int64_t remaining = timeout_ms - elapsed;
        int wait_ms = static_cast<int>(
            remaining < kPollTickMs ? remaining : kPollTickMs);
        int rc = poll(&pfd, 1, wait_ms);
        if (rc < 0) {
            if (errno == EINTR) continue;
            child.kill();
            break;
        }
        if (rc == 0) continue;  // poll tick: re-check cancel/timeout

        ssize_t n = read(fd, buf, sizeof buf);
        if (n < 0) {
            if (errno == EINTR) continue;
            child.kill();
            break;
        }
        if (n == 0) break;  // EOF: child closed stdout

        carry.append(buf, static_cast<std::size_t>(n));
        feed_lines(carry, mapper);
    }

    // Trailing partial line (agent died mid-write): still worth feeding.
    if (!carry.empty()) mapper.feed_line(carry);
}

}  // namespace

AgentOutcome AgentProcess::run(const std::vector<std::string>& argv,
                               std::int64_t timeout_ms,
                               const std::atomic<bool>& cancel_flag,
                               ChunkMapper::Sink sink,
                               const std::string& cwd) {
    AgentOutcome out;
    if (argv.empty()) return out;

    UniqueFd read_end, write_end;
    if (!cloexec_pipe(read_end, write_end)) return out;

    // Child: stdout -> pipe, stdin -> /dev/null, stderr inherited. Declared
    // after the pipe ends, so on any exit from this function the child is
    // killed and reaped (~Child) before the descriptors close.
    Child child = spawn(argv, write_end.get(), cwd);
    if (!child.spawned()) return out;
    write_end.reset();
    out.spawned = true;

    // The sink can throw (a stream write to a vanished client, an encode
    // refusal). ~Child still kills and reaps the child on that path, where
    // it would otherwise linger forever blocked on a full pipe; the
    // exception then carries on to the caller.
    ChunkMapper mapper(std::move(sink));
    pump(read_end.get(), timeout_ms, cancel_flag, mapper, child, out);

    read_end.reset();
    if (auto status = child.wait()) out.exit_code = exit_code(*status);

    out.saw_result = mapper.saw_result();
    out.max_turns_reached = mapper.max_turns_reached();
    out.malformed_lines = mapper.malformed_lines();
    return out;
}

}  // namespace savannah
