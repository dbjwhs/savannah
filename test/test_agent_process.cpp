// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Unit tests: what the daemon makes of a NodeConfig (the agent argv), the
// process helpers under the agent supervisor (UniqueFd, spawn, Child,
// AgentProcess::run with real short-lived children), the failure reasons
// and the task state names. No daemon, no fake-claude.

#include "agent_process.hpp"
#include "config.hpp"
#include "task_supervisor.hpp"
#include "test_support.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

using savannah::NodeConfig;


// The argv every agent invocation starts with is pinned element for element:
// savannahd's ask and the task supervisor both build on it, and the real
// claude CLI is what reads it.
static void test_base_agent_argv() {
    using Argv = std::vector<std::string>;

    NodeConfig nc;  // defaults: claude, no extra args, no tool list, 25 turns
    CHECK(savannah::base_agent_argv(nc, "hi there") ==
          (Argv{"claude", "-p", "hi there", "--output-format", "stream-json",
                "--verbose", "--max-turns", "25"}));

    nc.agent_cmd = "./fake-claude";
    nc.agent_args = {"--scenario", "echo"};
    nc.allowed_tools = {"Read", "Grep", "Bash(git *)"};
    nc.max_turns = 5;
    CHECK(savannah::base_agent_argv(nc, "") ==
          (Argv{"./fake-claude", "--scenario", "echo", "-p", "",
                "--output-format", "stream-json", "--verbose", "--max-turns",
                "5", "--allowedTools", "Read,Grep,Bash(git *)"}));

    // One tool: no separator.
    nc.allowed_tools = {"Read"};
    CHECK(savannah::base_agent_argv(nc, "p").back() == "Read");

    // to_cargv: the same strings, NULL-terminated, for exec.
    const Argv args = {"git", "-C", "/tmp"};
    std::vector<char*> cargv = savannah::to_cargv(args);
    CHECK(cargv.size() == 4);
    CHECK(std::string(cargv[0]) == "git");
    CHECK(std::string(cargv[2]) == "/tmp");
    CHECK(cargv[3] == nullptr);
}

// The reason in the synthetic error trailer, by precedence: spawn failed,
// timeout, cancelled, died before result.
static void test_failure_reason() {
    using savannah::AgentOutcome;
    using savannah::failure_reason;
    auto outcome = [](bool spawned, bool timed_out, bool killed) {
        AgentOutcome o;
        o.spawned = spawned;
        o.timed_out = timed_out;
        o.killed = killed;
        return o;
    };
    CHECK(std::string(failure_reason(outcome(false, false, false))) ==
          "spawn failed");
    CHECK(std::string(failure_reason(outcome(false, true, true))) ==
          "spawn failed");
    CHECK(std::string(failure_reason(outcome(true, true, false))) == "timeout");
    CHECK(std::string(failure_reason(outcome(true, true, true))) == "timeout");
    CHECK(std::string(failure_reason(outcome(true, false, true))) ==
          "cancelled");
    CHECK(std::string(failure_reason(outcome(true, false, false))) ==
          "died before result");
}

// The five state names are a contract with the dash and the shim, which
// match on the exact strings.
static void test_task_state_names() {
    using savannah::TaskState;
    using savannah::to_string;
    CHECK(std::string(to_string(TaskState::Idle)) == "idle");
    CHECK(std::string(to_string(TaskState::Running)) == "running");
    CHECK(std::string(to_string(TaskState::Incomplete)) == "incomplete");
    CHECK(std::string(to_string(TaskState::Failed)) == "failed");
    CHECK(std::string(to_string(TaskState::Cancelled)) == "cancelled");
}

static void test_unique_fd() {
    int fds[2];
    CHECK(pipe(fds) == 0);
    {
        savannah::UniqueFd a(fds[0]);
        savannah::UniqueFd b(fds[1]);
        CHECK(a.get() == fds[0]);
        savannah::UniqueFd moved(std::move(a));
        CHECK(a.get() == -1);
        CHECK(moved.get() == fds[0]);
        b.reset();
        CHECK(b.get() == -1);
        CHECK(fcntl(fds[1], F_GETFD) == -1);  // reset closed it
        CHECK(fcntl(fds[0], F_GETFD) != -1);  // still held by `moved`
    }
    CHECK(fcntl(fds[0], F_GETFD) == -1);  // the destructor closed it
}

// spawn + Child: exit codes come back through wait(), a killed child maps
// to 128 + SIGKILL, and the exec and chdir failures keep their codes.
static void test_spawn_and_child() {
    using savannah::exit_code;
    using savannah::kSilent;
    using savannah::spawn;
    auto code = [](savannah::Child child) {
        auto status = child.wait();
        return status ? exit_code(*status) : -1;
    };
    CHECK(code(spawn({"sh", "-c", "exit 3"}, kSilent)) == 3);
    CHECK(code(spawn({"sh", "-c", "exit 0"}, kSilent)) == 0);
    CHECK(code(spawn({"/nonexistent/savannah-test-binary"}, kSilent)) == 127);
    CHECK(code(spawn({"sh", "-c", "exit 0"}, kSilent,
                     "/nonexistent/savannah-test-dir")) == 126);
    CHECK(!spawn({}, kSilent).spawned());

    savannah::Child sleeper = spawn({"sleep", "30"}, kSilent);
    CHECK(sleeper.spawned());
    sleeper.kill();
    auto status = sleeper.wait();
    CHECK(status.has_value());
    CHECK(status && exit_code(*status) == 128 + 9);
    CHECK(!sleeper.spawned());        // reaped
    CHECK(!sleeper.wait().has_value());  // nothing left to reap

    // A Child dropped without wait() is killed and reaped by its destructor:
    // this returns at once instead of after the sleep.
    const auto start = std::chrono::steady_clock::now();
    { savannah::Child dropped = spawn({"sleep", "30"}, kSilent); }
    CHECK(std::chrono::steady_clock::now() - start < std::chrono::seconds(10));
}

// A sink that throws: the exception reaches the caller, and only after the
// agent has been killed and reaped (it would otherwise sleep on for 30 s).
static void test_run_reaps_when_sink_throws() {
    const std::string line =
        R"({"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}})";
    std::atomic<bool> cancel{false};
    savannah::AgentProcess proc;
    bool threw = false;
    const auto start = std::chrono::steady_clock::now();
    try {
        proc.run({"sh", "-c", "echo '" + line + "'; exec sleep 30"}, 60000,
                 cancel, [](const savannah::Chunk&) {
                     throw std::runtime_error("client went away");
                 });
    } catch (const std::runtime_error& e) {
        threw = std::string(e.what()) == "client went away";
    }
    CHECK(threw);
    CHECK(std::chrono::steady_clock::now() - start < std::chrono::seconds(10));
}

// The plain outcomes of a run, without a daemon around it: exit code, the
// 128 + signal mapping on timeout and cancel, a spawn that cannot exec.
static void test_run_outcomes() {
    auto run = [](const std::vector<std::string>& argv, std::int64_t timeout_ms,
                  bool cancelled) {
        std::atomic<bool> cancel{cancelled};
        savannah::AgentProcess proc;
        return proc.run(argv, timeout_ms, cancel, [](const savannah::Chunk&) {});
    };
    auto died = run({"sh", "-c", "exit 3"}, 60000, false);
    CHECK(died.spawned && !died.saw_result && died.exit_code == 3);
    CHECK(!died.timed_out && !died.killed);

    auto slow = run({"sleep", "30"}, 200, false);
    CHECK(slow.spawned && slow.timed_out && slow.exit_code == 128 + 9);

    auto stopped = run({"sleep", "30"}, 60000, true);
    CHECK(stopped.spawned && stopped.killed && stopped.exit_code == 128 + 9);

    auto missing = run({"/nonexistent/savannah-test-binary"}, 60000, false);
    CHECK(missing.spawned && missing.exit_code == 127);

    CHECK(!run({}, 60000, false).spawned);
}

int main() {
    test_base_agent_argv();
    test_failure_reason();
    test_task_state_names();
    test_unique_fd();
    test_spawn_and_child();
    test_run_reaps_when_sink_throws();
    test_run_outcomes();
    return savannah::test::finish("test_agent_process");
}
