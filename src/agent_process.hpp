// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// agent_process.hpp — spawn and supervise one headless agent invocation.
//
// v1 semantics: fork/exec the configured command, pump stdout lines into a
// ChunkMapper until EOF, result, timeout, or kill. The child's stderr is
// inherited (savannahd's own stderr) so operator logs stay in one place.

#pragma once

#include <sys/types.h>  // pid_t

#include <atomic>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "stream_json.hpp"

namespace savannah {

struct NodeConfig;

struct AgentOutcome {
    bool spawned = false;      // exec happened
    bool saw_result = false;   // stream ended with a RESULT chunk
    bool timed_out = false;
    bool killed = false;       // cancel() arrived
    bool max_turns_reached = false;  // RESULT subtype was error_max_turns
    int exit_code = -1;
    int malformed_lines = 0;
};

/// Why a run ended without a RESULT chunk: the `reason` of the synthetic
/// error trailer. Precedence: spawn failed, timeout, cancelled, died.
const char* failure_reason(const AgentOutcome& outcome);

/// The argv every agent invocation starts with: the configured command and
/// its extra args, then -p PROMPT, stream-json output, the turn cap and the
/// allowed tools when any are configured. Callers append their own flags.
std::vector<std::string> base_agent_argv(const NodeConfig& config,
                                         const std::string& prompt);

/// A NULL-terminated char* view of `argv` for the exec family. The pointers
/// borrow from `argv`, which must outlive the returned vector; exec never
/// writes through them, which is what makes the const_cast sound.
inline std::vector<char*> to_cargv(const std::vector<std::string>& argv) {
    std::vector<char*> cargv;
    cargv.reserve(argv.size() + 1);
    for (const auto& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
    cargv.push_back(nullptr);
    return cargv;
}

/// An owned file descriptor: closed when it goes out of scope.
class UniqueFd {
public:
    UniqueFd() = default;
    explicit UniqueFd(int fd) : fd_(fd) {}
    ~UniqueFd() { reset(); }

    UniqueFd(UniqueFd&& other) noexcept : fd_(other.release()) {}
    UniqueFd& operator=(UniqueFd&& other) noexcept {
        if (this != &other) reset(other.release());
        return *this;
    }
    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;

    int get() const { return fd_; }
    /// Give the descriptor up without closing it.
    int release() {
        int fd = fd_;
        fd_ = -1;
        return fd;
    }
    /// Close what is held (if anything) and hold `fd` instead.
    void reset(int fd = -1);

private:
    int fd_ = -1;
};

/// A forked child process. Whoever holds it must reap it: wait() does that
/// and hands back the status; a Child destroyed before wait() (an exception
/// unwinding past it) is sent SIGKILL and reaped there, so no path leaves a
/// zombie or a child blocked forever on a full pipe.
class Child {
public:
    Child() = default;
    explicit Child(pid_t pid) : pid_(pid) {}
    ~Child();

    Child(Child&& other) noexcept : pid_(other.pid_) { other.pid_ = -1; }
    Child& operator=(Child&&) = delete;
    Child(const Child&) = delete;
    Child& operator=(const Child&) = delete;

    /// False when the fork failed (or the child was already reaped).
    bool spawned() const { return pid_ > 0; }

    /// SIGKILL the child. It still has to be reaped (wait() or destruction).
    void kill();

    /// Block until the child exits and reap it. The raw waitpid status (see
    /// exit_code()), or nullopt when there was nothing to reap.
    std::optional<int> wait();

private:
    pid_t pid_ = -1;
};

/// A waitpid status as one number: the exit code, 128 + the signal number
/// for a child a signal killed, -1 for anything else.
int exit_code(int wait_status);

/// spawn()'s stdout argument for a child nobody listens to: stdout and
/// stderr both go to /dev/null.
inline constexpr int kSilent = -1;

/// fork + exec `argv` (argv[0] looked up on PATH). The child's stdin is
/// /dev/null. Its stdout is `stdout_fd` with stderr inherited, or, for
/// kSilent, both are /dev/null. `cwd`, if non-empty, is its working
/// directory: a chdir that fails ends the child with _exit(126), an exec
/// that fails with _exit(127). A failed fork returns a Child that is not
/// spawned().
Child spawn(const std::vector<std::string>& argv, int stdout_fd,
            const std::string& cwd = "");

class AgentProcess {
public:
    /// Run `argv` (argv[0] = binary), stream chunks into `sink`.
    /// Blocks until the child is done or timeout_ms expires.
    /// `cancel_flag` checked between reads; when set, child gets SIGKILL.
    /// `cwd`, if non-empty, is the child's working directory (chdir before
    /// exec) — task workers run inside their git worktree this way.
    AgentOutcome run(const std::vector<std::string>& argv,
                     std::int64_t timeout_ms,
                     const std::atomic<bool>& cancel_flag,
                     ChunkMapper::Sink sink,
                     const std::string& cwd = "");
};

}  // namespace savannah
