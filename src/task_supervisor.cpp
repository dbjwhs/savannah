// MIT License
// Copyright (c) 2026 Dennis B Jones

#include "task_supervisor.hpp"

#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <random>
#include <thread>

#include "agent_process.hpp"

namespace savannah {

namespace {

// 8-4-4-4-12 lowercase hex, claude-session-id shaped. No libuuid (zero-dep).
std::string new_session_id() {
    std::random_device rd;
    std::mt19937_64 gen(rd());
    std::uniform_int_distribution<int> hex(0, 15);
    static const char* d = "0123456789abcdef";
    std::string s;
    for (int i = 0; i < 32; ++i) {
        if (i == 8 || i == 12 || i == 16 || i == 20) s.push_back('-');
        s.push_back(d[hex(gen)]);
    }
    return s;
}

// How often tail() looks for new chunks while a turn is running.
constexpr std::chrono::milliseconds kTailPollInterval{50};

// Run a git command silently; return its exit code (-1 on spawn failure).
int run_git(const std::vector<std::string>& args) {
    // argv is built before the fork: the child only execs.
    std::vector<std::string> argv = {"git"};
    argv.insert(argv.end(), args.begin(), args.end());
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            dup2(devnull, STDIN_FILENO);
        }
        std::vector<char*> cargv = to_cargv(argv);
        execvp(cargv[0], cargv.data());
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) != pid) return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128;
}

}  // namespace

TaskSupervisor::~TaskSupervisor() {
    std::lock_guard<std::mutex> lock(mu_);
    for (auto& [id, t] : tasks_) {
        t->cancel->store(true);
        if (t->worker.joinable()) t->worker.join();
    }
}

TaskSupervisor::Found TaskSupervisor::find(const std::string& id) {
    Found found{std::unique_lock<std::mutex>(mu_), nullptr};
    auto it = tasks_.find(id);
    if (it != tasks_.end()) found.task = it->second;
    return found;
}

void TaskSupervisor::prune_worktree(Task& t) {
    if (t.branch.empty()) return;
    run_git({"-C", t.workdir, "worktree", "remove", "--force", t.worktree});
    run_git({"-C", t.workdir, "branch", "-D", t.branch});
    t.branch.clear();  // pruned once; a later cancel/remove must not prune again
}

TaskView TaskSupervisor::snapshot(Task& t) {
    TaskView v;
    v.id = t.id;
    v.title = t.title;
    v.state = t.state;
    v.worktree = t.worktree;
    v.session_id = t.session_id;
    v.turns = t.turns;
    v.last_line = t.last_line;
    return v;
}

void TaskSupervisor::drive_turn(Task* t, const std::string& prompt) {
    // One logical turn (one task_send) auto-continues across claude's
    // per-invocation --max-turns cap: it keeps resuming the session until the
    // agent finishes on its own, dies, is cancelled, or exhausts the
    // auto-continue budget. Without this a long task stalls the instant it hits
    // --max-turns and the caller has to hand-send "continue" every time (the
    // friction the enum-to-string delegation run surfaced). The task stays
    // "running" for the whole loop; only the final outcome sets the end state.
    constexpr int kMaxAutoContinues = 4;  // bounds token burn on a runaway task

    std::string turn_prompt = prompt;
    bool saw_result = false;
    bool max_turns = false;
    int auto_continues = 0;

    for (;;) {
        bool first;
        std::int64_t timeout;
        std::string cwd, session;
        {
            std::lock_guard<std::mutex> lock(t->m);
            first = (t->turns == 0);
            timeout = t->timeout_ms;
            cwd = t->worktree;
            session = t->session_id;
            // Delimit every resumed invocation (a new task_send, or an
            // auto-continue past the max-turns cap) in the transcript so the
            // tail shows turn boundaries instead of running the text together.
            // The task's very first invocation needs no leading separator.
            if (!first) {
                t->transcript.push_back(
                    {ChunkKind::Text,
                     "\n\n----- turn " + std::to_string(t->turns + 1) +
                         " -----\n"});
            }
        }

        std::vector<std::string> argv = base_agent_argv(config_, turn_prompt);
        // Workers have no human to answer prompts; acceptEdits lets them work
        // in their worktree without blocking, while risky ops still fail and
        // report.
        argv.push_back("--permission-mode");
        argv.push_back("acceptEdits");
        // First turn stamps the session id; every later turn (including an
        // auto-continue) resumes it.
        argv.push_back(first ? "--session-id" : "--resume");
        argv.push_back(session);

        AgentProcess proc;
        auto outcome = proc.run(
            argv, timeout, *t->cancel,
            [t](const Chunk& c) {
                std::lock_guard<std::mutex> lock(t->m);
                t->transcript.push_back(c);
                if (c.kind == ChunkKind::Text && !c.payload.empty()) {
                    t->last_line = c.payload;
                }
            },
            cwd);

        {
            std::lock_guard<std::mutex> lock(t->m);
            if (!outcome.saw_result) {
                // An explicit cancel reports as cancelled whatever else the
                // outcome says (a cancel that lands before the spawn included).
                std::string reason = t->cancel->load()
                                         ? "cancelled"
                                         : failure_reason(outcome);
                t->transcript.push_back(
                    {ChunkKind::Result,
                     ChunkMapper::synthetic_error_result(reason,
                                                         outcome.exit_code)});
            }
            ++t->turns;
        }

        saw_result = outcome.saw_result;
        max_turns = outcome.saw_result && outcome.max_turns_reached;

        // Continue only if the agent hit the turn cap with work still pending,
        // is not cancelled, produced a result, and we still have budget.
        if (t->cancel->load() || !saw_result || !max_turns) break;
        if (++auto_continues > kMaxAutoContinues) break;
        turn_prompt = "Continue where you left off.";
    }

    std::lock_guard<std::mutex> lock(t->m);
    t->state = t->cancel->load() ? "cancelled"
               : !saw_result     ? "failed"
               : max_turns        ? "incomplete"  // hit the auto-continue budget
                                   : "idle";
}

TaskView TaskSupervisor::create(const TaskCreate& spec) {
    std::lock_guard<std::mutex> lock(mu_);

    char idbuf[16];
    std::snprintf(idbuf, sizeof idbuf, "t-%04llu",
                  static_cast<unsigned long long>(next_++));
    std::string id = idbuf;

    auto task = std::make_shared<Task>();
    Task* t = task.get();
    t->id = id;
    t->title = spec.title.empty() ? id : spec.title;
    t->workdir = spec.workdir.empty() ? config_.workspace : spec.workdir;
    t->session_id = new_session_id();
    t->timeout_ms = spec.timeout_ms > 0 ? spec.timeout_ms : config_.timeout_ms;
    t->worktree = t->workdir;

    if (spec.make_worktree) {
        std::string branch = "savannah/" + id;
        std::string path = t->workdir + "/.savannah-worktrees/" + id;
        std::error_code ec;
        std::filesystem::create_directories(
            std::filesystem::path(path).parent_path(), ec);
        int rc = run_git({"-C", t->workdir, "worktree", "add", "-b", branch, path});
        if (rc == 0) {
            t->worktree = path;
            t->branch = branch;
        } else {
            t->state = "failed";
            t->last_line = "git worktree add failed (rc " + std::to_string(rc) + ")";
        }
    }

    if (t->state != "failed" && !spec.prompt.empty()) {
        t->state = "running";
        t->worker = std::thread(&TaskSupervisor::drive_turn, this, t, spec.prompt);
    }

    tasks_.emplace(id, std::move(task));
    std::lock_guard<std::mutex> tlock(t->m);
    return snapshot(*t);
}

std::vector<TaskView> TaskSupervisor::list() {
    std::lock_guard<std::mutex> lock(mu_);
    std::vector<TaskView> out;
    out.reserve(tasks_.size());
    for (auto& [id, t] : tasks_) {
        std::lock_guard<std::mutex> tlock(t->m);
        out.push_back(snapshot(*t));
    }
    return out;
}

TaskView TaskSupervisor::status(const std::string& id) {
    Found found = find(id);
    if (!found.task) return {};
    std::lock_guard<std::mutex> tlock(found.task->m);
    return snapshot(*found.task);
}

bool TaskSupervisor::send(const std::string& id, const std::string& prompt) {
    Found found = find(id);
    if (!found.task) return false;
    Task* t = found.task.get();
    {
        std::lock_guard<std::mutex> tlock(t->m);
        if (t->state == "running" || t->state == "cancelled") return false;
        t->state = "running";
    }
    if (t->worker.joinable()) t->worker.join();  // previous turn is done
    t->worker = std::thread(&TaskSupervisor::drive_turn, this, t, prompt);
    return true;
}

void TaskSupervisor::tail(const std::string& id,
                          const std::function<void(const Chunk&)>& emit) {
    // Hold a reference for the whole tail: remove() may drop the table entry
    // mid-replay, and this shared_ptr keeps the Task alive until we return.
    // mu_ is released as soon as the lookup returns.
    std::shared_ptr<Task> t = find(id).task;
    if (!t) {
        emit({ChunkKind::Result,
              ChunkMapper::synthetic_error_result("no such task", 0)});
        return;
    }
    // Snapshot then follow: drain everything already buffered, and while the
    // turn is running keep draining new chunks. State leaves "running" only
    // after the worker's final push, so a not-running observation is final.
    std::size_t idx = 0;
    for (;;) {
        bool running;
        {
            std::lock_guard<std::mutex> tlock(t->m);
            for (; idx < t->transcript.size(); ++idx) emit(t->transcript[idx]);
            running = (t->state == "running");
        }
        if (!running) return;
        std::this_thread::sleep_for(kTailPollInterval);
    }
}

bool TaskSupervisor::cancel(const std::string& id) {
    Found found = find(id);
    if (!found.task) return false;
    Task* t = found.task.get();
    t->cancel->store(true);
    if (t->worker.joinable()) t->worker.join();
    {
        std::lock_guard<std::mutex> tlock(t->m);
        t->state = "cancelled";
    }
    prune_worktree(*t);
    return true;
}

bool TaskSupervisor::remove(const std::string& id) {
    Found found = find(id);
    if (!found.task) return false;
    std::shared_ptr<Task> t = found.task;
    {
        std::lock_guard<std::mutex> tlock(t->m);
        if (t->state == "running") return false;  // cancel first
    }
    // Not running, and mu_ is held so no send() can start a new turn: the
    // worker thread (if any) is at or past its final state store. Join it
    // before the table drops its reference.
    if (t->worker.joinable()) t->worker.join();
    prune_worktree(*t);
    // A concurrent tail keeps its own shared_ptr; the table just forgets.
    tasks_.erase(id);
    return true;
}

}  // namespace savannah
