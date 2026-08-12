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

// Run a git command silently; return its exit code (-1 on spawn failure).
int run_git(const std::vector<std::string>& args) {
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            dup2(devnull, STDIN_FILENO);
        }
        std::vector<char*> cargv;
        cargv.push_back(const_cast<char*>("git"));
        for (const auto& a : args) cargv.push_back(const_cast<char*>(a.c_str()));
        cargv.push_back(nullptr);
        execvp("git", cargv.data());
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
    bool first;
    std::int64_t timeout;
    std::string cwd, session;
    {
        std::lock_guard<std::mutex> lock(t->m);
        first = (t->turns == 0);
        timeout = t->timeout_ms;
        cwd = t->worktree;
        session = t->session_id;
    }

    std::vector<std::string> argv;
    argv.push_back(config_.agent_cmd);
    for (const auto& a : config_.agent_args) argv.push_back(a);
    argv.push_back("-p");
    argv.push_back(prompt);
    argv.push_back("--output-format");
    argv.push_back("stream-json");
    argv.push_back("--verbose");
    argv.push_back("--max-turns");
    argv.push_back(std::to_string(config_.max_turns));
    if (!config_.allowed_tools.empty()) {
        argv.push_back("--allowedTools");
        std::string joined;
        for (const auto& tool : config_.allowed_tools) {
            if (!joined.empty()) joined += ",";
            joined += tool;
        }
        argv.push_back(joined);
    }
    // Workers have no human to answer prompts; acceptEdits lets them work in
    // their worktree without blocking, while risky ops still fail and report.
    argv.push_back("--permission-mode");
    argv.push_back("acceptEdits");
    // First turn stamps the session id; later turns resume it.
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

    std::lock_guard<std::mutex> lock(t->m);
    if (!outcome.saw_result) {
        std::string reason = t->cancel->load() ? "cancelled"
                             : outcome.timed_out ? "timeout"
                             : outcome.spawned   ? "died before result"
                                                 : "spawn failed";
        t->transcript.push_back(
            {ChunkKind::Result,
             ChunkMapper::synthetic_error_result(reason, outcome.exit_code)});
    }
    ++t->turns;
    t->state = t->cancel->load()   ? "cancelled"
               : outcome.saw_result ? "idle"
                                    : "failed";
}

TaskView TaskSupervisor::create(const TaskCreate& spec) {
    std::lock_guard<std::mutex> lock(mu_);

    char idbuf[16];
    std::snprintf(idbuf, sizeof idbuf, "t-%04llu",
                  static_cast<unsigned long long>(next_++));
    std::string id = idbuf;

    auto task = std::make_unique<Task>();
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
    std::lock_guard<std::mutex> lock(mu_);
    auto it = tasks_.find(id);
    if (it == tasks_.end()) return {};
    std::lock_guard<std::mutex> tlock(it->second->m);
    return snapshot(*it->second);
}

bool TaskSupervisor::send(const std::string& id, const std::string& prompt) {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = tasks_.find(id);
    if (it == tasks_.end()) return false;
    Task* t = it->second.get();
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
    Task* t = nullptr;
    {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = tasks_.find(id);
        if (it != tasks_.end()) t = it->second.get();
    }
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
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

bool TaskSupervisor::cancel(const std::string& id) {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = tasks_.find(id);
    if (it == tasks_.end()) return false;
    Task* t = it->second.get();
    t->cancel->store(true);
    if (t->worker.joinable()) t->worker.join();
    {
        std::lock_guard<std::mutex> tlock(t->m);
        t->state = "cancelled";
    }
    if (!t->branch.empty()) {
        run_git({"-C", t->workdir, "worktree", "remove", "--force", t->worktree});
        run_git({"-C", t->workdir, "branch", "-D", t->branch});
    }
    return true;
}

}  // namespace savannah
